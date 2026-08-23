#!/usr/bin/env python3
"""增量同步：只抓缺的，抓完驗證，驗證過才寫回 master。

三種情況（對應 _Manifest 分頁的「狀態」欄）：
  1. 新標的            → 抓滿 target_quarters 季 + 季末股價 + 預估
  2. 有資料但季數不足   → 只補缺的那幾季
  3. 已滿季且財報未到   → 季度 API 整個跳過，只更新股價
  4. 財報公布了        → 抓最新 1~2 季，把預估列翻成實際，並新增下一季預估列

「第一次全部補齊」與「之後只補缺的」是同一段程式碼 —— 前者只是「缺口 = 全部」
的特例。不寫成兩套，就不會有「第一次跑對了、增量跑錯了」這種難抓的 bug。
"""
import os, sys, argparse, datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

import sources

ROOT = Path(__file__).parent
DATA = ROOT / "data"
MASTER = DATA / "master.csv"          # 用 CSV 不用 parquet：git 可以 diff，
                                      # 財報被重編時 git log 直接告訴你哪天變的

RAW_Q_COLS = ["ticker", "period", "fy", "fq", "period_end", "is_est", "est_source",
              "eps_basis", "revenue", "gross_profit", "eps_diluted_adj",
              "shares_diluted", "total_equity", "dps", "price_at_end"]


def log(msg):
    print(msg, flush=True)


# ───────────────────────── 缺口判斷 ─────────────────────────
def plan(ticker, master, meta, cfg, today):
    have = master[master.ticker == ticker] if len(master) else master
    actual = have[have.is_est == "N"] if len(have) else have
    target = cfg["target_quarters"]
    if len(actual) == 0:
        return dict(mode="backfill", quarters=target, why="新標的，抓滿歷史")
    if len(actual) < target:
        return dict(mode="backfill", quarters=target,
                    why=f"季數不足（{len(actual)}/{target}），補歷史")
    ner = meta.get(ticker, {}).get("next_earnings")
    if ner and today <= ner + dt.timedelta(days=cfg["report_lag_days"]):
        return dict(mode="price_only", quarters=0,
                    why=f"財報日 {ner} 未到，跳過季度資料")
    return dict(mode="incremental", quarters=cfg["restatement_window"],
                why="財報可能已公布，抓最近幾季")


# ───────────────────────── 瀑布抓取 ─────────────────────────
def waterfall_fin(ticker, n, chain):
    """依序嘗試各來源，後面的只填前面缺的期別（不覆寫已有值）。"""
    out = sources.empty(sources.FIN_COLS)
    used = []
    for src in chain:
        if getattr(src, "needs_key", False) and not os.getenv("AV_API_KEY"):
            continue
        try:
            df = src.quarterly_financials(ticker, n)
        except Exception as e:
            log(f"      ! {src.name} 財報失敗: {e}")
            continue
        if df is None or df.empty:
            continue
        used.append(src.name)
        if out.empty:
            out = df.copy()
        else:
            out = out.drop_duplicates(subset=["period_end"]).set_index("period_end")
            df = df.drop_duplicates(subset=["period_end"]).set_index("period_end")
            out = out.combine_first(df).reset_index()
            for c in ("fy", "fq"):
                out[c] = pd.to_numeric(out[c], errors="coerce")
        # 每個數值欄都補滿了就不用再往下找
        if out[["revenue", "gross_profit", "shares_diluted",
                "total_equity"]].notna().all().all() and len(out) >= n:
            break
    return out, used


def waterfall_eps(ticker, n, chain):
    """街頭口徑優先；全部失敗才退回 GAAP，並回傳 basis 讓上層標記。"""
    for src in chain:
        if getattr(src, "needs_key", False) and not os.getenv("AV_API_KEY"):
            continue
        try:
            df = src.quarterly_eps_street(ticker, n)
        except Exception:
            df = None
        if df is not None and not df.empty:
            return df, "street", src.name
    for src in chain:
        try:
            df = src.quarterly_eps_gaap(ticker, n)
        except Exception:
            df = None
        if df is not None and not df.empty:
            log(f"      ⚠ {ticker} 取不到街頭口徑 EPS，降級為 GAAP（已標記 eps_basis=gaap）")
            return df, "gaap", src.name
    return sources.empty(sources.EPS_COLS), "street", None


def is_blank(v):
    """型別安全的「空」判斷。
    不能寫成 `v != {}` 或 `if v:` —— pandas 的 Series / DataFrame 會做逐元素比較，
    再丟給 and / if 就會拋 'truth value of a Series is ambiguous'。"""
    if v is None:
        return True
    if isinstance(v, (pd.Series, pd.DataFrame, pd.Index)):
        return len(v) == 0
    if isinstance(v, (dict, list, tuple, set, str)):
        return len(v) == 0
    return False


def first_of(chain, fname, *args):
    for src in chain:
        if getattr(src, "needs_key", False) and not os.getenv("AV_API_KEY"):
            continue
        fn = getattr(src, fname, None)
        if fn is None:
            continue
        try:
            v = fn(*args)
        except Exception as e:
            log(f"      ! {src.name}.{fname} 失敗: {e}")
            continue
        if not is_blank(v):
            return v, src.name
    return None, None


def merged_estimates(chain, ticker):
    """預估要跨來源合併（yq 有 0q 與家數、yf 只有 forwardEps 保底）。"""
    out = {}
    for src in chain:
        if getattr(src, "needs_key", False) and not os.getenv("AV_API_KEY"):
            continue
        fn = getattr(src, "estimates", None)
        if not fn:
            continue
        try:
            for k, v in (fn(ticker) or {}).items():
                out.setdefault(k, v)
        except Exception:
            continue
    return out


# ───────────────────────── 組成 Raw_Q 列 ─────────────────────────
def nearest_price(prices, when):
    if prices is None or len(prices) == 0:
        return np.nan
    idx = pd.to_datetime(pd.Series(prices.index)).dt.date
    ok = idx[idx <= when]
    if ok.empty:
        return np.nan
    return float(prices.iloc[ok.index[-1]])


def build_rows(ticker, fin, eps, basis, divs, prices, cfg):
    # combine_first 合併不同來源後，缺漏的期別會讓 fy / fq 變 NaN，int() 會炸。
    fin = fin.dropna(subset=["period_end", "fy", "fq"])
    fin = fin.sort_values("period_end", ascending=False)
    fin = fin.head(cfg["target_quarters"]).copy()
    epsmap = {}
    if eps is not None and not eps.empty:
        # EPS 的期末日可能與財報表差幾天，用最接近的一季對齊
        for _, r in eps.iterrows():
            epsmap[r["period_end"]] = float(r["eps"])

    def eps_for(pe):
        if pe in epsmap:
            return epsmap[pe]
        cand = [d for d in epsmap if abs((d - pe).days) <= 12]
        return epsmap[min(cand, key=lambda d: abs((d - pe).days))] if cand else np.nan

    ends = sorted(fin["period_end"].tolist(), reverse=True)
    rows = []
    for _, r in fin.iterrows():
        pe = r["period_end"]
        prev = next((d for d in ends if d < pe), None)
        dps = np.nan
        if divs is not None and len(divs) and prev is not None:
            d_idx = pd.to_datetime(pd.Series(divs.index)).dt.date
            sel = divs[(d_idx > prev).values & (d_idx <= pe).values]
            dps = float(sel.sum()) if len(sel) else 0.0
        rows.append(dict(
            ticker=ticker, period=f"FY{int(r.fy)}Q{int(r.fq)}",
            fy=int(r.fy), fq=int(r.fq), period_end=pe,
            is_est="N", est_source="actual", eps_basis=basis,
            revenue=r.revenue, gross_profit=r.gross_profit,
            eps_diluted_adj=eps_for(pe), shares_diluted=r.shares_diluted,
            total_equity=r.total_equity, dps=dps,
            price_at_end=nearest_price(prices, pe)))
    return pd.DataFrame(rows, columns=RAW_Q_COLS)


def estimate_row(ticker, actual_rows, est, basis):
    """當季（尚未公布）的預估列 —— seq 會在 Excel 端算成 0。"""
    if actual_rows.empty or not est.get("eps_q0"):
        return sources.empty(RAW_Q_COLS)
    ok = actual_rows.dropna(subset=["fy", "fq"])
    if ok.empty:
        return sources.empty(RAW_Q_COLS)
    last = ok.sort_values("period_end", ascending=False).iloc[0]
    fy, fq = int(last.fy), int(last.fq) + 1
    if fq > 4:
        fy, fq = fy + 1, 1
    pe = last.period_end + dt.timedelta(days=91)
    return pd.DataFrame([dict(
        ticker=ticker, period=f"FY{fy}Q{fq}E", fy=fy, fq=fq, period_end=pe,
        is_est="Y", est_source="consensus", eps_basis=basis,
        revenue=np.nan, gross_profit=np.nan,
        eps_diluted_adj=float(est["eps_q0"]), shares_diluted=np.nan,
        total_equity=np.nan, dps=np.nan, price_at_end=np.nan)],
        columns=RAW_Q_COLS)


# ───────────────────────── 驗證 ─────────────────────────
def validate(df, cfg):
    errs = []
    a = df[df.is_est == "N"]
    dup = a.duplicated(subset=["ticker", "fy", "fq"]).sum()
    if dup:
        errs.append(f"{dup} 筆 (ticker, fy, fq) 重複")
    bad = a[(a.revenue <= 0) | a.revenue.isna()]
    if len(bad):
        errs.append(f"{len(bad)} 筆 revenue 缺失或非正數："
                    f"{bad[['ticker','period']].head(5).to_dict('records')}")
    for tk, g in a.groupby("ticker"):
        g = g.sort_values("period_end")
        d = (g.fy * 4 + g.fq).diff().dropna()
        if (d == 0).any():
            errs.append(f"{tk} 有兩筆對到同一個財季（期別換算可能出錯）")
        big = d[d > 1]
        if len(big):
            errs.append(f"{tk} 季度有缺口，跳過了 {int(big.sum() - len(big))} 季")
    n_gaap = (df.eps_basis == "gaap").sum()
    warn = [f"{n_gaap} 筆 EPS 降級為 GAAP 口徑"] if n_gaap else []
    return errs, warn


# ───────────────────────── 主流程 ─────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="只跑指定代號（逗號分隔），用於除錯")
    ap.add_argument("--force-full", action="store_true", help="忽略快取，全部重抓")
    ap.add_argument("--dry-run", action="store_true", help="不寫檔，只印出計畫")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    chain = sources.chain(cfg["sources"])
    today = dt.date.today()
    DATA.mkdir(exist_ok=True)

    tickers = pd.read_csv(ROOT / "tickers.csv")
    tickers = tickers[tickers.ticker.notna()]
    if args.only:
        keep = {s.strip().upper() for s in args.only.split(",")}
        tickers = tickers[tickers.ticker.str.upper().isin(keep)]

    if MASTER.exists() and not args.force_full:
        master = pd.read_csv(MASTER, parse_dates=["period_end"])
        master["period_end"] = master["period_end"].dt.date
    else:
        master = pd.DataFrame(columns=RAW_Q_COLS)
    meta_path = DATA / "meta.csv"
    meta_df = pd.read_csv(meta_path, parse_dates=["next_earnings"]) if meta_path.exists() \
        else pd.DataFrame()
    if len(meta_df):
        meta_df["next_earnings"] = meta_df["next_earnings"].dt.date
    meta = {r.ticker: dict(next_earnings=r.next_earnings)
            for r in meta_df.itertuples()} if len(meta_df) else {}

    est_rows, price_rows, meta_rows, all_new = [], [], [], []
    calls = 0

    for tk in tickers.ticker:
        p = plan(tk, master, meta, cfg, today)
        log(f"  {tk:<8} {p['mode']:<12} {p['why']}")
        if args.dry_run:
            continue

        ner, _ = first_of(chain, "next_earnings", tk)
        est = merged_estimates(chain, tk)
        prices, _ = first_of(chain, "price_history", tk, None)
        px = float(prices.iloc[-1]) if prices is not None and len(prices) else np.nan
        meta_rows.append(dict(ticker=tk, next_earnings=ner, checked=today))
        price_rows.append(dict(ticker=tk, price=px, price_date=today,
                               next_earnings=ner, as_of=today))
        est_rows.append(dict(ticker=tk, eps_f1=est.get("eps_f1"), eps_f2=est.get("eps_f2"),
                             fy1_end=est.get("fy1_end"),
                             n_analysts=est.get("n_analysts"), as_of=today))
        calls += 3

        if p["mode"] == "price_only":
            continue

        fin, used = waterfall_fin(tk, p["quarters"], chain)
        if fin.empty:
            log(f"      ! {tk} 完全抓不到季度財報，跳過")
            continue
        eps, basis, esrc = waterfall_eps(tk, p["quarters"], chain)
        divs, _ = first_of(chain, "dividends", tk, None)
        rows = build_rows(tk, fin, eps, basis, divs, prices, cfg)
        rows = pd.concat([rows, estimate_row(tk, rows, est, basis)], ignore_index=True)
        all_new.append(rows)
        calls += len(used) + 2
        log(f"      財報={'+'.join(used)}  EPS={esrc}({basis})  {len(rows)} 列")

    if args.dry_run:
        log("\n(dry-run，未寫檔)")
        return 0

    if all_new:
        new = pd.concat(all_new, ignore_index=True)
        keys = ["ticker", "fy", "fq"]
        master = (pd.concat([new, master], ignore_index=True)
                  .drop_duplicates(subset=keys, keep="first"))   # 新的覆蓋舊的
    master = master.sort_values(["ticker", "period_end"], ascending=[True, False])

    errs, warn = validate(master, cfg)
    for w in warn:
        log(f"  ⚠ {w}")
    if errs:
        log("\n✗ 驗證未通過，master 未更新：")
        for e in errs:
            log(f"    - {e}")
        return 1

    master.to_csv(MASTER, index=False)
    pd.DataFrame(meta_rows).to_csv(meta_path, index=False)
    master.to_csv(DATA / "raw_q.csv", index=False)
    pd.DataFrame(est_rows).to_csv(DATA / "raw_est.csv", index=False)
    pd.DataFrame(price_rows).to_csv(DATA / "raw_price.csv", index=False)
    tickers.to_csv(DATA / "tickers.csv", index=False)
    log(f"\n✓ 完成：{master.ticker.nunique()} 檔 / {len(master)} 列 / 約 {calls} 次 API 呼叫")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
# seq / key 由這裡算好一起輸出，Excel 端就不需要任何公式欄，Power Query 可以直接載入。
# 安全性：sync 每次都會重寫整份 master 並重新排序，所以補進新一季時 seq 一定是對的。
OUT_COLS = RAW_Q_COLS + ["seq", "key"]


def add_seq(df):
    """seq：is_est="Y" 記為 0（當季預估）；實際季由新到舊排 1、2、3…
    key：ticker|seq，給 Excel 端做文字型 INDEX/MATCH。"""
    if df.empty:
        out = df.copy()
        out["seq"] = []
        out["key"] = []
        return out[OUT_COLS]
    df = df.copy()
    actual = df.is_est == "N"
    df["seq"] = 0
    df.loc[actual, "seq"] = (df[actual]
                             .groupby("ticker")["period_end"]
                             .rank(ascending=False, method="first")
                             .astype(int))
    df["key"] = df.ticker.astype(str) + "|" + df.seq.astype(str)
    return df[OUT_COLS]


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
            # 用 (fy, fq) 當鍵，不能用 period_end ——
            # 不同來源給同一季的期末日會差好幾天（yfinance 給月底、SEC 給 52/53 週制實際日），
            # 用日期當鍵會讓同一季被當成兩季，歷史永遠補不滿。
            out = out.dropna(subset=["fy", "fq"]).drop_duplicates(subset=["fy", "fq"])
            df = df.dropna(subset=["fy", "fq"]).drop_duplicates(subset=["fy", "fq"])
            out = (out.set_index(["fy", "fq"])
                   .combine_first(df.set_index(["fy", "fq"]))
                   .reset_index())
        # 每個數值欄都補滿了就不用再往下找
        if out[["revenue", "gross_profit", "shares_diluted",
                "total_equity"]].notna().all().all() and len(out) >= n:
            break
    return out, used


def _merge_eps(a, b):
    if a.empty:
        return b.copy()
    a = a.drop_duplicates(subset=["period_end"]).set_index("period_end")
    b = b.drop_duplicates(subset=["period_end"]).set_index("period_end")
    return a.combine_first(b).reset_index()


def waterfall_eps(ticker, n, chain):
    """街頭口徑優先，而且**跨來源互補** —— 不是拿到第一個非空就停。

    MSFT 的 yfinance earnings_dates 會回空表；若停在第一層，整檔 EPS 就會全空
    （yahooquery / AV 其實有資料）。這裡沿用財報那邊「只補缺的」同一套邏輯。
    不同來源的 index 語意不同（公布日 vs 期末日）沒關係，align_eps 會各自處理。
    """
    got, used = sources.empty(sources.EPS_COLS), []
    for src in chain:
        if getattr(src, "needs_key", False) and not os.getenv("AV_API_KEY"):
            continue
        try:
            df = src.quarterly_eps_street(ticker, n)
        except Exception as e:
            log(f"      ! {src.name} 街頭 EPS 失敗: {e}")
            continue
        if df is None or df.empty:
            continue
        used.append(src.name)
        got = _merge_eps(got, df)
        if len(got) >= n:
            break
    if not got.empty:
        return got, "street", "+".join(used)

    for src in chain:
        try:
            df = src.quarterly_eps_gaap(ticker, n)
        except Exception:
            df = None
        if df is not None and not df.empty:
            log(f"      ⚠ {ticker} 取不到街頭口徑 EPS，降級為 GAAP")
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


AMBIGUOUS_EST = {"yf"}   # yf 的 forwardEps 沒有標明是哪個財年，只能當保底


def merged_estimates(chain, ticker):
    """預估要跨來源合併（yq 有 0q 與家數、yf 只有 forwardEps 保底）。

    重點：yf 的 info["forwardEps"] 其實常常是「下一個財年」而不是本財年，
    如果讓它先佔走 eps_f1，就會出現 eps_f1 == eps_f2（都是下一年），
    害 EPS成長_F ≈ 0 而 PEG_F 全部變成 N/M。
    所以有標明期別的來源（yq / av）先填，yf 只在缺口時補。
    """
    labeled, fallback = {}, {}
    for src in chain:
        if getattr(src, "needs_key", False) and not os.getenv("AV_API_KEY"):
            continue
        fn = getattr(src, "estimates", None)
        if not fn:
            continue
        try:
            got = fn(ticker) or {}
        except Exception:
            continue
        target = fallback if src.name in AMBIGUOUS_EST else labeled
        for k, v in got.items():
            if v is not None:
                target.setdefault(k, v)

    out = dict(labeled)
    for k, v in fallback.items():
        out.setdefault(k, v)

    # 只有一邊有值時互補，讓 PE Forward 還算得出來；
    # 此時 f1 == f2 → 成長率 0 → PEG_F 誠實顯示 N/M。
    if out.get("eps_f1") is None and out.get("eps_f2") is not None:
        out["eps_f1"] = out["eps_f2"]
    if out.get("eps_f2") is None and out.get("eps_f1") is not None:
        out["eps_f2"] = out["eps_f1"]
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


def align_eps(eps_df, ends):
    """把 EPS 對到財季 —— 來源的 index 有兩種語意，必須分開處理：

      A. 期末日型（yahooquery earnings_history / AV EARNINGS 的 fiscalDateEnding）
         → 直接對期末日，容許 52/53 週制的幾天偏移。
      B. 公布日型（yfinance earnings_dates）→ index 是「財報公布日」，
         通常比期末日晚 20~40 天。用 ±12 天去比對會全部落空
         （這就是 AAPL / MSFT / NVDA 整欄 EPS 空白的原因）。
         正確作法：把每筆公布值指派給「公布日之前最近的那個期末日」。
    """
    if eps_df is None or eps_df.empty:
        return {}
    asc = sorted(ends)
    out = {}
    for _, r in eps_df.iterrows():
        d, v = r["period_end"], r["eps"]
        if pd.isna(v) or d is None:
            continue
        near = [pe for pe in asc if abs((d - pe).days) <= 12]
        if near:                                   # A. 期末日型
            pe = min(near, key=lambda p: abs((d - p).days))
        else:                                      # B. 公布日型
            prior = [pe for pe in asc if 5 <= (d - pe).days <= 120]
            if not prior:
                continue
            pe = max(prior)
        gap = abs((d - pe).days)
        if pe not in out or gap < out[pe][1]:      # 同一季多筆時取最接近的
            out[pe] = (float(v), gap)
    return {k: v[0] for k, v in out.items()}


SPLIT_FACTORS = (2, 3, 4, 5, 6, 8, 10, 15, 20)


def fix_split_shares(rows):
    """修正股數的分割斷層。

    yfinance 的損益表對分割前的季度不一定回溯調整，同一檔會出現 2.5B 與 24.9B
    並存（NVDA 2024/6 的 10:1）。但 EPS 是調整後的口徑，兩者相乘反推淨利會差十倍，
    ROE 直接爆掉。

    以最新一季為基準，凡是倍率落在常見分割比例 ±4% 內的值就換算回同一基準。
    正常的回購／增發漂移（六年頂多 ±20%）不會誤觸。
    """
    if rows.empty or rows.shares_diluted.notna().sum() == 0:
        return rows
    ref = rows.shares_diluted.dropna().iloc[0]     # rows 已由新到舊排序
    out, n_fix = rows.shares_diluted.copy(), 0
    for i2, v in rows.shares_diluted.items():
        if pd.isna(v) or v <= 0:
            continue
        r = ref / v
        for f in SPLIT_FACTORS:
            if abs(r - f) / f < 0.04:
                out[i2] = v * f
                n_fix += 1
                break
            if abs(r - 1.0 / f) * f < 0.04:
                out[i2] = v / f
                n_fix += 1
                break
    if n_fix:
        rows = rows.copy()
        rows["shares_diluted"] = out
        log(f"      ⚙ 修正 {n_fix} 季的股數分割斷層（換算到最新一季的基準）")
    return rows


def build_rows(ticker, fin, eps, basis, divs, prices, cfg):
    # combine_first 合併不同來源後，缺漏的期別會讓 fy / fq 變 NaN，int() 會炸。
    fin = fin.dropna(subset=["period_end", "fy", "fq"])
    fin = fin.sort_values("period_end", ascending=False)
    fin = fin.head(cfg["target_quarters"]).copy()
    ends = sorted(fin["period_end"].tolist(), reverse=True)
    epsmap = align_eps(eps, ends)

    def eps_for(pe):
        return epsmap.get(pe, np.nan)

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
    return fix_split_shares(pd.DataFrame(rows, columns=RAW_Q_COLS))


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
def sanitize(df):
    """把「不可用」和「會算錯」的列處理掉，而不是把整批更新擋下來。

    原本的作法是驗證不過就整份不寫入 —— 結果 MRVL 少一筆營收、
    MU 歷史有個缺口，就把另外六檔正常的股票也一起卡住。太硬了。

    三種問題，三種處理：
      · 營收缺失或非正數 → 那一列什麼指標都撐不起來，直接丟掉
      · 季度有缺口       → 只留「最新的連續一段」。因為所有指標都是用 seq
                          （由新到舊的名次）去取區間，中間缺一季會讓 seq 5
                          不再是去年同期，YoY 會靜靜地算錯。寧可歷史短一點
      · 同一財季有兩列   → 期別換算出錯，會汙染 seq。這檔整個退回舊資料
    """
    notes, quarantine = [], set()
    est = df[df.is_est == "Y"]
    act = df[df.is_est == "N"].copy()

    bad = act[act.revenue.isna() | (act.revenue <= 0)]
    if len(bad):
        for r in bad.itertuples():
            notes.append(f"{r.ticker} {r.period} 沒有營收，丟掉這一列")
        act = act.drop(bad.index)

    keep = []
    for tk, g in act.groupby("ticker"):
        g = g.sort_values("period_end", ascending=False)
        idx = (g.fy * 4 + g.fq).tolist()
        if len(set(idx)) != len(idx):
            quarantine.add(tk)
            notes.append(f"{tk} 有兩筆對到同一個財季，這次的資料整批退回")
            continue
        cut = len(g)
        for i in range(1, len(idx)):
            if idx[i - 1] - idx[i] != 1:      # 由新到舊，正常是每次減 1
                cut = i
                break
        if cut < len(g):
            notes.append(f"{tk} 第 {cut + 1} 季往前有缺口，只保留最近 {cut} 季"
                         f"（保留缺口前的資料會讓 YoY 對錯季）")
        keep.append(g.head(cut))

    act = pd.concat(keep, ignore_index=True) if keep else act.iloc[0:0]
    out = pd.concat([act, est[~est.ticker.isin(quarantine)]], ignore_index=True)

    n_gaap = int((out.eps_basis == "gaap").sum())
    if n_gaap:
        notes.append(f"{n_gaap} 筆 EPS 降級為 GAAP 口徑")
    a = out[out.is_est == "N"]
    n_miss = int(a.eps_diluted_adj.isna().sum())
    if n_miss:
        notes.append(f"{n_miss}/{len(a)} 季沒有對到 EPS")
    return out, notes, quarantine


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
        divs, dsrc = first_of(chain, "dividends", tk, None)
        rows = build_rows(tk, fin, eps, basis, divs, prices, cfg)
        n_eps = int(rows[rows.is_est == "N"].eps_diluted_adj.notna().sum())
        n_act = int((rows.is_est == "N").sum())
        n_div = 0 if divs is None else len(divs)
        log(f"      EPS 對上 {n_eps}/{n_act} 季 ｜ 股利事件 {n_div} 筆 "
            f"（{dsrc or '無'}）｜ 股價 {0 if prices is None else len(prices)} 筆")
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
        # 新的原則上覆蓋舊的（財報會被追溯重編），但有一個例外：
        # 這次抓回來是空的、master 裡原本卻有值，那就是來源當下的一次性缺漏，
        # 不能讓它把好資料洗掉。這種洞會一路傳染 —— 少一季就變成缺口，
        # 缺口前面的歷史又會被整段截掉。
        if len(master):
            usable = master.revenue.notna() & (master.revenue > 0)
            have_good = set(map(tuple, master.loc[usable, keys].values))
            hollow = new.revenue.isna() | (new.revenue <= 0)
            clash = [tuple(v) in have_good for v in new[keys].values]
            drop = new[hollow & pd.Series(clash, index=new.index)]
            if len(drop):
                for r in drop.itertuples():
                    log(f"      ⚠ {r.ticker} {r.period} 這次抓到空的，沿用既有資料")
                new = new.drop(drop.index)
        master = (pd.concat([new, master], ignore_index=True)
                  .drop_duplicates(subset=keys, keep="first"))   # 新的覆蓋舊的
    master = master.sort_values(["ticker", "period_end"], ascending=[True, False])

    before = len(master)
    prev = pd.read_csv(MASTER) if MASTER.exists() else None
    master, notes, quarantine = sanitize(master)
    for n in notes:
        log(f"  ⚠ {n}")
    if quarantine and prev is not None:
        # 被隔離的股票沿用上一版的資料，不要因為這次抓壞就整檔消失
        old = prev[prev.ticker.isin(quarantine)]
        if len(old):
            old["period_end"] = pd.to_datetime(old["period_end"]).dt.date
            master = pd.concat([master, old], ignore_index=True)
            log(f"  ⚠ {'、'.join(sorted(quarantine))} 沿用上一版資料")
    if len(master) < before:
        log(f"  ⚠ 共清掉 {before - len(master)} 列有問題的資料")
    if master.empty:
        log("\n✗ 清理後沒有任何資料可寫，master 保持原狀")
        return 1

    def keep_others(new_rows, path):
        """--only 只跑部分股票時，沒跑到的那些要沿用舊資料，不能被整份蓋掉。
        （master 本來就是合併的，但 meta / est / price 原本是直接覆寫。）"""
        new = pd.DataFrame(new_rows)
        if path.exists():
            old = pd.read_csv(path)
            if len(new) and "ticker" in old.columns:
                old = old[~old.ticker.isin(new.ticker)]
            new = pd.concat([new, old], ignore_index=True)
        return new.sort_values("ticker") if "ticker" in new.columns else new

    master.to_csv(MASTER, index=False)
    keep_others(meta_rows, meta_path).to_csv(meta_path, index=False)
    add_seq(master).to_csv(DATA / "raw_q.csv", index=False)
    keep_others(est_rows, DATA / "raw_est.csv").to_csv(DATA / "raw_est.csv", index=False)
    keep_others(price_rows, DATA / "raw_price.csv").to_csv(DATA / "raw_price.csv", index=False)
    # tickers.csv 要寫「完整的那份」，不是被 --only 篩過的
    pd.read_csv(ROOT / "tickers.csv").to_csv(DATA / "tickers.csv", index=False)
    log(f"\n✓ 完成：{master.ticker.nunique()} 檔 / {len(master)} 列 / 約 {calls} 次 API 呼叫")
    return 0


if __name__ == "__main__":
    sys.exit(main())

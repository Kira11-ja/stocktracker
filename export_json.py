#!/usr/bin/env python3
"""把 data/*.csv 轉成網頁用的 docs/data.json。

刻意只輸出「原始欄位」，不輸出算好的指標。
所有衍生指標（PE、PEG、加速度、ROE…）都在網頁上用 JS 現算，
這樣你在網頁上改門檻設定時，整頁立刻跟著變，不必等 GitHub 重跑。
單位與 Excel 一致：營收／毛利／股東權益／股數都除以 100 萬。
"""
import json
import math
import re
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"
MILLIONS = ["revenue", "gross_profit", "total_equity", "shares_diluted"]
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")   # 擋掉 tickers.csv 裡的說明文字那一列


def as_int(v):
    v = clean(v)
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def clean(v):
    """NaN / NaT / pandas 型別 → JSON 認得的東西。"""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if pd.isna(v):
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()[:10]
    if hasattr(v, "item"):
        v = v.item()
    return v


def read(name, dates=()):
    p = DATA / name
    if not p.exists():
        return pd.DataFrame()
    df = pd.read_csv(p)
    for c in dates:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce")
    return df


def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())

    q = read("raw_q.csv", dates=["period_end"])
    est = read("raw_est.csv", dates=["fy1_end", "as_of"])
    price = read("raw_price.csv", dates=["price_date", "next_earnings", "as_of"])
    tick = read("tickers.csv", dates=["加入日期", "added"])

    for c in MILLIONS:
        if c in q.columns:
            q[c] = pd.to_numeric(q[c], errors="coerce") / 1e6

    quarters = []
    for r in q.to_dict("records"):
        quarters.append({
            "t": clean(r.get("ticker")),
            "period": clean(r.get("period")),
            "fy": as_int(r.get("fy")),
            "fq": as_int(r.get("fq")),
            "end": clean(r.get("period_end")),
            "est": clean(r.get("is_est")) == "Y",
            "src": clean(r.get("est_source")),
            "basis": clean(r.get("eps_basis")),
            "rev": clean(r.get("revenue")),
            "gp": clean(r.get("gross_profit")),
            "eps": clean(r.get("eps_diluted_adj")),
            "sh": clean(r.get("shares_diluted")),
            "eq": clean(r.get("total_equity")),
            "dps": clean(r.get("dps")),
            "px": clean(r.get("price_at_end")),
            "seq": as_int(r.get("seq")),
        })

    est_map = {}
    for r in est.to_dict("records"):
        t = clean(r.get("ticker"))
        if not t:
            continue
        est_map[t] = {"f1": clean(r.get("eps_f1")), "f2": clean(r.get("eps_f2")),
                      "fy1_end": clean(r.get("fy1_end")),
                      "n": clean(r.get("n_analysts"))}

    px_map = {}
    for r in price.to_dict("records"):
        t = clean(r.get("ticker"))
        if not t:
            continue
        px_map[t] = {"price": clean(r.get("price")),
                     "price_date": clean(r.get("price_date")),
                     "next_earnings": clean(r.get("next_earnings")),
                     "as_of": clean(r.get("as_of"))}

    tickers = []
    for r in tick.to_dict("records"):
        t = clean(r.get("ticker"))
        if not t or not SYMBOL.match(str(t).strip().upper()):
            continue
        t = str(t).strip().upper()
        tickers.append({
            "t": t,
            "name": clean(r.get("公司名稱")) or clean(r.get("name")) or t,
            "industry": clean(r.get("產業")) or clean(r.get("industry")) or "",
            "tier": clean(r.get("等級")) or clean(r.get("tier")) or "池子",
            "added": clean(r.get("加入日期")) or clean(r.get("added")) or "",
            "note": clean(r.get("備註")) or clean(r.get("note")) or "",
        })

    as_of = max([v["as_of"] for v in px_map.values() if v["as_of"]], default="")

    out = {
        "generated": as_of,
        "defaults": {
            "target_quarters": int(cfg.get("target_quarters", 24)),
            "gm_avg_quarters": int(cfg.get("gm_avg_quarters", 20)),
            "peg_min_growth": float(cfg.get("peg_min_growth", 0.05)),
            "min_analysts": int(cfg.get("min_analysts", 5)),
            "ntm_days": int(cfg.get("ntm_days", 365)),
        },
        "tickers": tickers,
        "quarters": quarters,
        "est": est_map,
        "price": px_map,
    }

    DOCS.mkdir(exist_ok=True)
    (DOCS / "data.json").write_text(
        json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    kb = (DOCS / "data.json").stat().st_size / 1024
    print(f"✓ docs/data.json：{len(tickers)} 檔 / {len(quarters)} 季 / {kb:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

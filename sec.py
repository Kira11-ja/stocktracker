"""第 4 層：SEC EDGAR XBRL。免金鑰、最權威，但只有 GAAP、而且最慢，所以擺最後。

★ 修正了既有 Colab 版本的一個嚴重錯誤 ★
原本的寫法是：
    if r["form"] in ["10-K", ...]: res[int(r["fy"])] = r["val"]
companyfacts 裡的 fy / fp 是「這筆事實出現在哪一份報告」，不是「這筆數字屬於哪一期」。
一份 10-K 同時含有三年的損益表數字，三筆的 fy 都一樣 —— 迴圈會一路覆蓋，
最後留下哪一年完全看順序，資料會被隨機汙染而且不會報錯。

正確作法：用每筆事實自己的 start / end 判斷期間，再以 end 當 key。
"""
import os
import pandas as pd
import numpy as np
import requests
from . import base

name = "sec"
needs_key = False
UA = os.getenv("SEC_USER_AGENT", "stock-tracker/1.0 (contact@example.com)")
HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}
_map = None
_facts = {}


def _cik(ticker):
    global _map
    if _map is None:
        try:
            js = requests.get("https://www.sec.gov/files/company_tickers.json",
                              headers=HEADERS, timeout=30).json()
            _map = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in js.values()}
        except Exception:
            _map = {}
    return _map.get(ticker.upper().replace("-", "."), _map.get(ticker.upper()))


def _companyfacts(ticker):
    if ticker in _facts:
        return _facts[ticker]
    cik = _cik(ticker)
    js = None
    if cik:
        try:
            js = requests.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
                              headers=HEADERS, timeout=45).json()
        except Exception:
            js = None
    _facts[ticker] = js
    return js


def _pull(js, tags, unit_pred, lo_days, hi_days):
    """取出期間長度落在 [lo_days, hi_days] 的事實，以 end 日期為 key。
    duration 型（營收、EPS）給季度或年度天數；instant 型（權益）用 lo=hi=None。"""
    out = {}
    facts = ((js or {}).get("facts") or {}).get("us-gaap") or {}
    for tag in tags:
        for unit, arr in ((facts.get(tag) or {}).get("units") or {}).items():
            if not unit_pred(unit):
                continue
            for it in arr:
                end = it.get("end")
                val = it.get("val")
                if not end or val is None:
                    continue
                if lo_days is not None:
                    start = it.get("start")
                    if not start:
                        continue
                    dur = (pd.Timestamp(end) - pd.Timestamp(start)).days
                    if not (lo_days <= dur <= hi_days):
                        continue
                d = pd.Timestamp(end).date()
                # 同一期間出現多筆（不同報告）→ 取 filed 最新的，等於採用重編後的值
                prev = out.get(d)
                if prev is None or it.get("filed", "") >= prev[1]:
                    out[d] = (float(val), it.get("filed", ""))
    return {k: v[0] for k, v in out.items()}


def quarterly_financials(ticker, n=24):
    js = _companyfacts(ticker)
    if not js:
        return base.empty(base.FIN_COLS)
    from . import yf as _yf
    usd = lambda u: "usd" in u.lower() and "share" not in u.lower()
    shares = lambda u: "share" in u.lower()
    rev = _pull(js, ["RevenueFromContractWithCustomerExcludingAssessedTax",
                     "Revenues", "SalesRevenueNet"], usd, 80, 100)
    gp = _pull(js, ["GrossProfit"], usd, 80, 100)
    sh = _pull(js, ["WeightedAverageNumberOfDilutedSharesOutstanding"], shares, 80, 100)
    eq = _pull(js, ["StockholdersEquity",
                    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
               usd, None, None)
    m = _yf.fy_end_month(ticker)
    rows = []
    for d in sorted(rev, reverse=True)[:n]:
        fy, fq = base.fiscal_from_period_end(d, m)
        rows.append(dict(period_end=d, fy=fy, fq=fq,
                         revenue=rev.get(d, np.nan), gross_profit=gp.get(d, np.nan),
                         shares_diluted=sh.get(d, np.nan), total_equity=eq.get(d, np.nan)))
    return pd.DataFrame(rows, columns=base.FIN_COLS)


def quarterly_eps_gaap(ticker, n=24):
    js = _companyfacts(ticker)
    if not js:
        return base.empty(base.EPS_COLS)
    per_share = lambda u: "share" in u.lower() and "usd" in u.lower()
    eps = _pull(js, ["EarningsPerShareDiluted", "EarningsPerShareBasic"], per_share, 80, 100)
    rows = [dict(period_end=d, eps=eps[d]) for d in sorted(eps, reverse=True)[:n]]
    return pd.DataFrame(rows, columns=base.EPS_COLS)

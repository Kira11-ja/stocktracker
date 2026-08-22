"""第 3 層：Alpha Vantage。需要金鑰（環境變數 AV_API_KEY）。
免費方案有每日次數限制，所以只在前兩層補不齊時才呼叫，且結果做程序內快取
—— 這就是既有 Colab 的 _AV_CACHE 作法，原封保留。"""
import os, time
import pandas as pd
import numpy as np
import requests
from . import base

name = "av"
needs_key = True
BASE = "https://www.alphavantage.co/query"
DELAY = float(os.getenv("AV_DELAY_SEC", "15"))
_cache = {}


def _key():
    return os.getenv("AV_API_KEY", "").strip()


def _get(fn, ticker):
    if not _key():
        return None
    ck = (fn, ticker)
    if ck in _cache:
        return _cache[ck]
    try:
        r = requests.get(BASE, params={"function": fn, "symbol": ticker,
                                       "apikey": _key()}, timeout=25)
        js = r.json() if r.status_code == 200 else None
        if isinstance(js, dict) and ("Note" in js or "Information" in js):
            js = None                      # 觸到頻率上限
    except Exception:
        js = None
    _cache[ck] = js
    time.sleep(DELAY)
    return js


def quarterly_financials(ticker, n=24):
    inc = _get("INCOME_STATEMENT", ticker)
    bal = _get("BALANCE_SHEET", ticker)
    if not inc:
        return base.empty(base.FIN_COLS)
    from . import yf as _yf
    m = _yf.fy_end_month(ticker)
    eq = {}
    for it in (bal or {}).get("quarterlyReports", []):
        d = it.get("fiscalDateEnding", "")[:10]
        v = it.get("totalShareholderEquity") or it.get("totalStockholdersEquity")
        if d and v not in (None, "None"):
            eq[d] = _f(v)
    rows = []
    for it in inc.get("quarterlyReports", [])[:n]:
        d = it.get("fiscalDateEnding", "")[:10]
        if not d:
            continue
        pe = pd.to_datetime(d).date()
        fy, fq = base.fiscal_from_period_end(pe, m)
        rows.append(dict(period_end=pe, fy=fy, fq=fq,
                         revenue=_f(it.get("totalRevenue")),
                         gross_profit=_f(it.get("grossProfit")),
                         shares_diluted=np.nan,
                         total_equity=eq.get(d, np.nan)))
    return pd.DataFrame(rows, columns=base.FIN_COLS)


def _f(v):
    try:
        if v in (None, "None", ""):
            return np.nan
        x = float(v)
        return x if np.isfinite(x) else np.nan
    except Exception:
        return np.nan


def quarterly_eps_street(ticker, n=24):
    """EARNINGS 端點的 reportedEPS 是「向市場報告」的口徑，也就是 adjusted。
    既有 Colab 已經在用，只是當成第三順位；這裡維持同樣的位置。"""
    js = _get("EARNINGS", ticker)
    if not js:
        return base.empty(base.EPS_COLS)
    rows = []
    for it in js.get("quarterlyEarnings", [])[:n]:
        d = it.get("fiscalDateEnding", "")[:10]
        e = _f(it.get("reportedEPS"))
        if d and not np.isnan(e):
            rows.append(dict(period_end=pd.to_datetime(d).date(), eps=e))
    return pd.DataFrame(rows, columns=base.EPS_COLS)

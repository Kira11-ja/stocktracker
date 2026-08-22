"""第 1 層：yfinance。免金鑰，覆蓋最廣，優先使用。"""
import pandas as pd
import numpy as np
from . import base
from ._util import to_dt_cols, series_of, REV, GP, COGS, SHARES, EQUITY, EPS_DIL, EPS_BAS

name = "yf"
needs_key = False


def _t(ticker):
    import yfinance as yf
    for sym in base.symbol_candidates(ticker):
        t = yf.Ticker(sym)
        try:
            q = t.quarterly_income_stmt
            if q is not None and len(q) > 0:
                return t
        except Exception:
            continue
    import yfinance as yf
    return yf.Ticker(ticker)


def fy_end_month(ticker):
    """由最近一次年報的期末月份推出財年結束月 —— 用來把 period_end 換算成 (fy, fq)。"""
    try:
        inc = to_dt_cols(_t(ticker).income_stmt)
        if inc is not None and len(inc.columns):
            return int(pd.Timestamp(inc.columns[0]).month)
    except Exception:
        pass
    return 12


def quarterly_financials(ticker, n=24):
    try:
        t = _t(ticker)
        inc = to_dt_cols(t.quarterly_income_stmt)
        bs = to_dt_cols(t.quarterly_balance_sheet)
        if inc is None:
            return base.empty(base.FIN_COLS)
        rev = series_of(inc, REV)
        gp = series_of(inc, GP)
        if gp is None:
            cogs = series_of(inc, COGS)
            gp = (rev - cogs) if (rev is not None and cogs is not None) else None
        sh = series_of(inc, SHARES)
        eq = series_of(bs, EQUITY) if bs is not None else None
        m = fy_end_month(ticker)
        rows = []
        for c in list(inc.columns)[:n]:
            pe = pd.Timestamp(c).date()
            fy, fq = base.fiscal_from_period_end(pe, m)
            rows.append(dict(
                period_end=pe, fy=fy, fq=fq,
                revenue=_g(rev, c), gross_profit=_g(gp, c),
                shares_diluted=_g(sh, c),
                total_equity=_g(eq, c) if eq is not None else np.nan))
        return pd.DataFrame(rows, columns=base.FIN_COLS)
    except Exception:
        return base.empty(base.FIN_COLS)


def _g(ser, c):
    try:
        v = float(ser[c])
        return v if np.isfinite(v) else np.nan
    except Exception:
        return np.nan


def quarterly_eps_street(ticker, n=24):
    """earnings_dates 的 'Reported EPS' 是街頭口徑（adjusted），不是損益表的 GAAP。
    這是本專案取得 adjusted EPS 的第一順位，免金鑰。"""
    try:
        t = _t(ticker)
        df = t.get_earnings_dates(limit=n + 8)
        if df is None or len(df) == 0:
            return base.empty(base.EPS_COLS)
        col = next((c for c in df.columns if "reported" in str(c).lower()), None)
        if col is None:
            return base.empty(base.EPS_COLS)
        out = df[[col]].dropna().reset_index()
        out.columns = ["period_end", "eps"]
        out["period_end"] = pd.to_datetime(out["period_end"], utc=True).dt.date
        return out.head(n)
    except Exception:
        return base.empty(base.EPS_COLS)


def quarterly_eps_gaap(ticker, n=24):
    try:
        inc = to_dt_cols(_t(ticker).quarterly_income_stmt)
        ser = series_of(inc, EPS_DIL) or series_of(inc, EPS_BAS)
        if ser is None:
            return base.empty(base.EPS_COLS)
        ser = ser.dropna()
        return pd.DataFrame({"period_end": [pd.Timestamp(c).date() for c in ser.index],
                             "eps": ser.values})[:n]
    except Exception:
        return base.empty(base.EPS_COLS)


def dividends(ticker, since=None):
    try:
        d = _t(ticker).dividends
        if since is not None and len(d):
            d = d[d.index.date >= since]
        return d
    except Exception:
        return pd.Series(dtype="float64")


def price_history(ticker, since=None):
    try:
        h = _t(ticker).history(period="max", auto_adjust=False)
        if h is None or len(h) == 0:
            return pd.Series(dtype="float64")
        s = h["Close"]
        if since is not None:
            s = s[s.index.date >= since]
        return s
    except Exception:
        return pd.Series(dtype="float64")


def estimates(ticker):
    """優先用 yahooquery 的 earnings_trend（那裡才有 0q 當季預估與分析師家數），
    yfinance 這層只做保底。"""
    out = {}
    try:
        info = _t(ticker).info or {}
        if info.get("forwardEps"):
            out["eps_f1"] = float(info["forwardEps"])
    except Exception:
        pass
    return out


def next_earnings(ticker):
    try:
        cal = _t(ticker).calendar
        v = None
        if isinstance(cal, dict):
            v = cal.get("Earnings Date")
        if isinstance(v, (list, tuple)) and v:
            v = v[0]
        return pd.Timestamp(v).date() if v is not None else None
    except Exception:
        return None

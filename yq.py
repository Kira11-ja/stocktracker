"""第 2 層：yahooquery。免金鑰。財報欄位偶爾比 yfinance 完整，
而且 earnings_trend 是取得「當季共識預估 + 分析師家數」最好的地方。"""
import pandas as pd
import numpy as np
from . import base

name = "yq"
needs_key = False


def _yq(ticker):
    from yahooquery import Ticker
    return Ticker(ticker, formatted=False)


def _pick(df, cands):
    for c in cands:
        if c in df.columns:
            return c
    return None


def quarterly_financials(ticker, n=24):
    from . import yf as _yf
    for sym in base.symbol_candidates(ticker):
        try:
            yq = _yq(sym)
            inc = yq.income_statement(frequency="q")
            bs = yq.balance_sheet(frequency="q")
            if not isinstance(inc, pd.DataFrame) or inc.empty:
                continue
            inc = inc.reset_index()
            bs = bs.reset_index() if isinstance(bs, pd.DataFrame) and not bs.empty else None
            rc = _pick(inc, ["TotalRevenue", "totalRevenue", "OperatingRevenue"])
            gc = _pick(inc, ["GrossProfit", "grossProfit"])
            sc = _pick(inc, ["DilutedAverageShares", "dilutedAverageShares"])
            if rc is None:
                continue
            eq = None
            if bs is not None:
                ec = _pick(bs, ["StockholdersEquity", "TotalEquityGrossMinorityInterest",
                                "totalStockholderEquity"])
                if ec:
                    eq = dict(zip(pd.to_datetime(bs["asOfDate"]).dt.date, bs[ec]))
            m = _yf.fy_end_month(ticker)
            rows = []
            for _, r in inc.sort_values("asOfDate", ascending=False).head(n).iterrows():
                pe = pd.to_datetime(r["asOfDate"]).date()
                fy, fq = base.fiscal_from_period_end(pe, m)
                rows.append(dict(
                    period_end=pe, fy=fy, fq=fq,
                    revenue=_f(r.get(rc)), gross_profit=_f(r.get(gc)) if gc else np.nan,
                    shares_diluted=_f(r.get(sc)) if sc else np.nan,
                    total_equity=_f(eq.get(pe)) if eq else np.nan))
            if rows:
                return pd.DataFrame(rows, columns=base.FIN_COLS)
        except Exception:
            continue
    return base.empty(base.FIN_COLS)


def _f(v):
    try:
        v = float(v)
        return v if np.isfinite(v) else np.nan
    except Exception:
        return np.nan


def quarterly_eps_street(ticker, n=24):
    """earnings_history 的 epsActual 同樣是街頭口徑。"""
    try:
        df = _yq(ticker).earnings_history
        if not isinstance(df, pd.DataFrame) or df.empty:
            return base.empty(base.EPS_COLS)
        df = df.reset_index()
        dc = _pick(df, ["quarter", "period_end", "startdatetime"])
        ec = _pick(df, ["epsActual", "epsactual"])
        if dc is None or ec is None:
            return base.empty(base.EPS_COLS)
        out = df[[dc, ec]].dropna()
        out.columns = ["period_end", "eps"]
        out["period_end"] = pd.to_datetime(out["period_end"]).dt.date
        return out.sort_values("period_end", ascending=False).head(n)
    except Exception:
        return base.empty(base.EPS_COLS)


def estimates(ticker):
    """0q = 當季、+1q = 下一季、0y/+1y = 本財年 / 下一財年。
    既有 Colab 已經在用這個端點，這裡把 numberOfAnalysts 一起帶出來。"""
    out = {}
    try:
        d = _yq(ticker).earnings_trend
        trend = (d.get(ticker) or {}).get("trend", []) if isinstance(d, dict) else []
        for r in trend:
            per = str(r.get("period", "")).lower()
            est = r.get("earningsEstimate") or {}
            avg = est.get("avg")
            if isinstance(avg, dict):
                avg = avg.get("raw")
            if avg is None:
                continue
            nan_ = est.get("numberOfAnalysts")
            if isinstance(nan_, dict):
                nan_ = nan_.get("raw")
            if per in ("0q", "currentquarter", "current"):
                out["eps_q0"] = float(avg)
                if nan_: out["n_analysts"] = int(nan_)
            elif per in ("0y", "currentyear"):
                out["eps_f1"] = float(avg)
                if nan_ and "n_analysts" not in out: out["n_analysts"] = int(nan_)
            elif per in ("+1y", "nextyear"):
                out["eps_f2"] = float(avg)
    except Exception:
        pass
    return out


def next_earnings(ticker):
    try:
        c = _yq(ticker).calendar_events
        v = (c.get(ticker) or {}).get("earnings", {}).get("earningsDate", [])
        if v:
            return pd.to_datetime(v[0]).date()
    except Exception:
        pass
    return None

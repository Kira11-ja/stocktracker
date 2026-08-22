"""資料來源瀑布 —— yfinance → yahooquery → Alpha Vantage → SEC EDGAR。

沿用原本 Colab 的分層想法，但補上兩條規則：
  1. 只補缺的：後面的來源只填前面沒有的期別，不覆寫已有的值。
  2. 同口徑遞補：街頭口徑（adjusted）之間可以互補；街頭全失敗才退回 GAAP，
     並在 eps_basis 欄留下記號，PE / PEG 不會被靜默污染。

★ 修正原 Colab 的兩個錯誤 ★
  A. SEC companyfacts 的 fy 是「這筆事實出現在哪份報告」，不是「屬於哪一期」。
     一份 10-K 含三年數字、三筆 fy 相同 → 原本的寫法會一路覆蓋，資料被隨機汙染
     而且不報錯。這裡改用每筆事實自己的 start / end 判斷期間。
  B. 財年換算：原本用 period_end.year，一月結帳的公司（NVDA）會整個錯開一年。
     這裡用 fiscal_from_period_end() 依財年結束月份換算。
"""
import os
import re
import time
import datetime as dt

import numpy as np
import pandas as pd
import requests

# ───────────────────────── 資料契約 ─────────────────────────
FIN_COLS = ["period_end", "fy", "fq", "revenue", "gross_profit",
            "shares_diluted", "total_equity"]
EPS_COLS = ["period_end", "eps"]


def empty(cols):
    return pd.DataFrame(columns=cols)


def symbol_candidates(raw):
    """代號變體 —— 沿用原 Colab 的處理（BRK.B / BRK-B / GOOG / GOOGL）。"""
    out, seen = [], set()

    def add(x):
        if x and x not in seen:
            out.append(x); seen.add(x)

    add(raw); add(raw.replace("-", ".")); add(raw.replace(".", "-"))
    u = raw.upper()
    if u in ("GOOG", "GOOGL"):
        add("GOOG"); add("GOOGL")
    if u.replace("-", ".") in ("BRK.B", "BRK.A"):
        add("BRK-B"); add("BRK.B"); add("BRK-A"); add("BRK.A")
    return out


def fiscal_from_period_end(period_end, fy_end_month):
    """由期末日與財年結束月份推出 (fy, fq)。

    NVDA 一月結帳：期末 2026-01-25 → FY2026Q4；2026-04-26 → FY2027Q1。
    AAPL 九月結帳：期末 2025-12-27 → FY2026Q1。
    """
    y, m = period_end.year, period_end.month
    delta = (fy_end_month - m) % 12
    fq = 4 - (delta // 3)
    fq = 4 if fq == 0 else fq
    fy = y + 1 if m > fy_end_month else y
    if fy_end_month <= 3 and m > fy_end_month:
        fy = y + 1
    return int(fy), int(fq)


# ───────────────────────── 欄名模糊比對 ─────────────────────────
# yfinance 的欄名會隨版本變動（Total Revenue / Revenue / Operating Revenue），
# 這段是原本 Colab 裡寫得最對的部分，原封保留。
def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def find_row(df, candidates):
    if df is None or len(df) == 0:
        return None
    m = {_norm(i): i for i in df.index}
    for c in candidates:
        k = _norm(c)
        if k in m:
            return m[k]
    for c in candidates:
        k = _norm(c)
        for kk, orig in m.items():
            if k in kk:
                return orig
    return None


REV = ["Total Revenue", "Revenue", "Operating Revenue", "Total Sales", "Net Sales"]
GP = ["Gross Profit", "GrossProfit", "Gross Income"]
COGS = ["Cost Of Revenue", "Cost of Revenue", "Cost Of Goods Sold"]
SHARES = ["Diluted Average Shares", "Weighted Average Diluted Shares",
          "Diluted Shares Outstanding"]
EQUITY = ["Total Stockholder Equity", "Total Stockholders Equity",
          "Total Stockholders' Equity", "Stockholders Equity",
          "Total Equity Gross Minority Interest", "Total Equity", "Common Stock Equity"]
EPS_DIL = ["Diluted EPS", "Earnings Per Share Diluted", "DilutedEPS"]
EPS_BAS = ["Basic EPS", "Earnings Per Share Basic"]


def _to_dt_cols(df):
    if df is None or len(df) == 0:
        return None
    out = df.copy()
    cols = []
    for c in out.columns:
        try:
            cols.append(pd.to_datetime(c))
        except Exception:
            cols.append(pd.NaT)
    out.columns = cols
    out = out.loc[:, ~pd.isna(out.columns)]
    return out.sort_index(axis=1, ascending=False)


def _ser(df, cands):
    r = find_row(df, cands)
    return None if r is None else pd.to_numeric(df.loc[r], errors="coerce")


def _num(ser, c):
    try:
        v = float(ser[c])
        return v if np.isfinite(v) else np.nan
    except Exception:
        return np.nan


def _f(v):
    try:
        if v in (None, "None", ""):
            return np.nan
        x = float(v)
        return x if np.isfinite(x) else np.nan
    except Exception:
        return np.nan


class Source:
    """沒有的能力回傳空表 / None，sync.py 會自動往下一層找。"""
    name = "base"
    needs_key = False

    @staticmethod
    def quarterly_financials(ticker, n=24): return empty(FIN_COLS)
    @staticmethod
    def quarterly_eps_street(ticker, n=24): return empty(EPS_COLS)
    @staticmethod
    def quarterly_eps_gaap(ticker, n=24): return empty(EPS_COLS)
    @staticmethod
    def dividends(ticker, since=None): return pd.Series(dtype="float64")
    @staticmethod
    def price_history(ticker, since=None): return pd.Series(dtype="float64")
    @staticmethod
    def estimates(ticker): return {}
    @staticmethod
    def next_earnings(ticker): return None


# ───────────────────────── ① yfinance ─────────────────────────
class YF(Source):
    name = "yf"
    needs_key = False

    @staticmethod
    def _t(ticker):
        import yfinance as yf
        for sym in symbol_candidates(ticker):
            t = yf.Ticker(sym)
            try:
                q = t.quarterly_income_stmt
                if q is not None and len(q) > 0:
                    return t
            except Exception:
                continue
        return yf.Ticker(ticker)

    @staticmethod
    def fy_end_month(ticker):
        """由最近一次年報的期末月份推出財年結束月。"""
        try:
            inc = _to_dt_cols(YF._t(ticker).income_stmt)
            if inc is not None and len(inc.columns):
                return int(pd.Timestamp(inc.columns[0]).month)
        except Exception:
            pass
        return 12

    @staticmethod
    def quarterly_financials(ticker, n=24):
        try:
            t = YF._t(ticker)
            inc = _to_dt_cols(t.quarterly_income_stmt)
            bs = _to_dt_cols(t.quarterly_balance_sheet)
            if inc is None:
                return empty(FIN_COLS)
            rev = _ser(inc, REV)
            gp = _ser(inc, GP)
            if gp is None:
                cogs = _ser(inc, COGS)
                gp = (rev - cogs) if (rev is not None and cogs is not None) else None
            sh = _ser(inc, SHARES)
            eq = _ser(bs, EQUITY) if bs is not None else None
            m = YF.fy_end_month(ticker)
            rows = []
            for c in list(inc.columns)[:n]:
                pe = pd.Timestamp(c).date()
                fy, fq = fiscal_from_period_end(pe, m)
                rows.append(dict(period_end=pe, fy=fy, fq=fq,
                                 revenue=_num(rev, c) if rev is not None else np.nan,
                                 gross_profit=_num(gp, c) if gp is not None else np.nan,
                                 shares_diluted=_num(sh, c) if sh is not None else np.nan,
                                 total_equity=_num(eq, c) if eq is not None else np.nan))
            return pd.DataFrame(rows, columns=FIN_COLS)
        except Exception:
            return empty(FIN_COLS)

    @staticmethod
    def quarterly_eps_street(ticker, n=24):
        """earnings_dates 的 'Reported EPS' 是街頭口徑（adjusted），不是損益表的 GAAP。
        這是取得 adjusted EPS 的第一順位，免金鑰。"""
        try:
            t = YF._t(ticker)
            df = t.get_earnings_dates(limit=n + 8)
            if df is None or len(df) == 0:
                return empty(EPS_COLS)
            col = next((c for c in df.columns if "reported" in str(c).lower()), None)
            if col is None:
                return empty(EPS_COLS)
            out = df[[col]].dropna().reset_index()
            out.columns = ["period_end", "eps"]
            out["period_end"] = pd.to_datetime(out["period_end"], utc=True).dt.date
            return out.head(n)
        except Exception:
            return empty(EPS_COLS)

    @staticmethod
    def quarterly_eps_gaap(ticker, n=24):
        try:
            inc = _to_dt_cols(YF._t(ticker).quarterly_income_stmt)
            ser = _ser(inc, EPS_DIL)
            if ser is None:
                ser = _ser(inc, EPS_BAS)
            if ser is None:
                return empty(EPS_COLS)
            ser = ser.dropna()
            return pd.DataFrame({"period_end": [pd.Timestamp(c).date() for c in ser.index],
                                 "eps": ser.values})[:n]
        except Exception:
            return empty(EPS_COLS)

    @staticmethod
    def dividends(ticker, since=None):
        try:
            d = YF._t(ticker).dividends
            if since is not None and len(d):
                d = d[d.index.date >= since]
            return d
        except Exception:
            return pd.Series(dtype="float64")

    @staticmethod
    def price_history(ticker, since=None):
        try:
            h = YF._t(ticker).history(period="max", auto_adjust=False)
            if h is None or len(h) == 0:
                return pd.Series(dtype="float64")
            s = h["Close"]
            if since is not None:
                s = s[s.index.date >= since]
            return s
        except Exception:
            return pd.Series(dtype="float64")

    @staticmethod
    def estimates(ticker):
        out = {}
        try:
            info = YF._t(ticker).info or {}
            if info.get("forwardEps"):
                out["eps_f1"] = float(info["forwardEps"])
        except Exception:
            pass
        return out

    @staticmethod
    def next_earnings(ticker):
        try:
            cal = YF._t(ticker).calendar
            v = cal.get("Earnings Date") if isinstance(cal, dict) else None
            if isinstance(v, (list, tuple)) and v:
                v = v[0]
            return pd.Timestamp(v).date() if v is not None else None
        except Exception:
            return None


# ───────────────────────── ② yahooquery ─────────────────────────
class YQ(Source):
    name = "yq"
    needs_key = False

    @staticmethod
    def _yq(ticker):
        from yahooquery import Ticker
        return Ticker(ticker, formatted=False)

    @staticmethod
    def _pick(df, cands):
        for c in cands:
            if c in df.columns:
                return c
        return None

    @staticmethod
    def quarterly_financials(ticker, n=24):
        for sym in symbol_candidates(ticker):
            try:
                yq = YQ._yq(sym)
                inc = yq.income_statement(frequency="q")
                bs = yq.balance_sheet(frequency="q")
                if not isinstance(inc, pd.DataFrame) or inc.empty:
                    continue
                inc = inc.reset_index()
                bs = bs.reset_index() if isinstance(bs, pd.DataFrame) and not bs.empty else None
                rc = YQ._pick(inc, ["TotalRevenue", "totalRevenue", "OperatingRevenue"])
                gc = YQ._pick(inc, ["GrossProfit", "grossProfit"])
                sc = YQ._pick(inc, ["DilutedAverageShares", "dilutedAverageShares"])
                if rc is None:
                    continue
                eq = None
                if bs is not None:
                    ec = YQ._pick(bs, ["StockholdersEquity",
                                       "TotalEquityGrossMinorityInterest",
                                       "totalStockholderEquity"])
                    if ec:
                        eq = dict(zip(pd.to_datetime(bs["asOfDate"]).dt.date, bs[ec]))
                m = YF.fy_end_month(ticker)
                rows = []
                for _, r in inc.sort_values("asOfDate", ascending=False).head(n).iterrows():
                    pe = pd.to_datetime(r["asOfDate"]).date()
                    fy, fq = fiscal_from_period_end(pe, m)
                    rows.append(dict(period_end=pe, fy=fy, fq=fq,
                                     revenue=_f(r.get(rc)),
                                     gross_profit=_f(r.get(gc)) if gc else np.nan,
                                     shares_diluted=_f(r.get(sc)) if sc else np.nan,
                                     total_equity=_f(eq.get(pe)) if eq else np.nan))
                if rows:
                    return pd.DataFrame(rows, columns=FIN_COLS)
            except Exception:
                continue
        return empty(FIN_COLS)

    @staticmethod
    def quarterly_eps_street(ticker, n=24):
        """earnings_history 的 epsActual 同樣是街頭口徑。"""
        try:
            df = YQ._yq(ticker).earnings_history
            if not isinstance(df, pd.DataFrame) or df.empty:
                return empty(EPS_COLS)
            df = df.reset_index()
            dc = YQ._pick(df, ["quarter", "period_end", "startdatetime"])
            ec = YQ._pick(df, ["epsActual", "epsactual"])
            if dc is None or ec is None:
                return empty(EPS_COLS)
            out = df[[dc, ec]].dropna()
            out.columns = ["period_end", "eps"]
            out["period_end"] = pd.to_datetime(out["period_end"]).dt.date
            return out.sort_values("period_end", ascending=False).head(n)
        except Exception:
            return empty(EPS_COLS)

    @staticmethod
    def estimates(ticker):
        """0q = 當季、0y / +1y = 本財年 / 下一財年。原 Colab 已在用這個端點，
        這裡把 numberOfAnalysts 與 endDate 一起帶出來。"""
        out = {}
        try:
            d = YQ._yq(ticker).earnings_trend
            trend = (d.get(ticker) or {}).get("trend", []) if isinstance(d, dict) else []
            for r in trend:
                per = str(r.get("period", "")).lower()
                est = r.get("earningsEstimate") or {}
                avg = est.get("avg")
                if isinstance(avg, dict):
                    avg = avg.get("raw")
                if avg is None:
                    continue
                na = est.get("numberOfAnalysts")
                if isinstance(na, dict):
                    na = na.get("raw")
                end = r.get("endDate")
                if per in ("0q", "currentquarter", "current"):
                    out["eps_q0"] = float(avg)
                    if na:
                        out["n_analysts"] = int(na)
                elif per in ("0y", "currentyear"):
                    out["eps_f1"] = float(avg)
                    if end:
                        try:
                            out["fy1_end"] = pd.to_datetime(end).date()
                        except Exception:
                            pass
                    if na and "n_analysts" not in out:
                        out["n_analysts"] = int(na)
                elif per in ("+1y", "nextyear"):
                    out["eps_f2"] = float(avg)
        except Exception:
            pass
        return out

    @staticmethod
    def next_earnings(ticker):
        try:
            c = YQ._yq(ticker).calendar_events
            v = (c.get(ticker) or {}).get("earnings", {}).get("earningsDate", [])
            if v:
                return pd.to_datetime(v[0]).date()
        except Exception:
            pass
        return None


# ───────────────────────── ③ Alpha Vantage ─────────────────────────
class AV(Source):
    """需要金鑰（環境變數 AV_API_KEY）。免費方案有次數限制，所以只在前兩層
    補不齊時才呼叫，且結果做程序內快取 —— 這就是原 Colab 的 _AV_CACHE。"""
    name = "av"
    needs_key = True
    BASE = "https://www.alphavantage.co/query"
    _cache = {}

    @staticmethod
    def _key():
        return os.getenv("AV_API_KEY", "").strip()

    @staticmethod
    def _get(fn, ticker):
        if not AV._key():
            return None
        ck = (fn, ticker)
        if ck in AV._cache:
            return AV._cache[ck]
        try:
            r = requests.get(AV.BASE, params={"function": fn, "symbol": ticker,
                                              "apikey": AV._key()}, timeout=25)
            js = r.json() if r.status_code == 200 else None
            if isinstance(js, dict) and ("Note" in js or "Information" in js):
                js = None                      # 觸到頻率上限
        except Exception:
            js = None
        AV._cache[ck] = js
        time.sleep(float(os.getenv("AV_DELAY_SEC", "15")))
        return js

    @staticmethod
    def quarterly_financials(ticker, n=24):
        inc = AV._get("INCOME_STATEMENT", ticker)
        bal = AV._get("BALANCE_SHEET", ticker)
        if not inc:
            return empty(FIN_COLS)
        m = YF.fy_end_month(ticker)
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
            fy, fq = fiscal_from_period_end(pe, m)
            rows.append(dict(period_end=pe, fy=fy, fq=fq,
                             revenue=_f(it.get("totalRevenue")),
                             gross_profit=_f(it.get("grossProfit")),
                             shares_diluted=np.nan,
                             total_equity=eq.get(d, np.nan)))
        return pd.DataFrame(rows, columns=FIN_COLS)

    @staticmethod
    def quarterly_eps_street(ticker, n=24):
        """EARNINGS 端點的 reportedEPS 是「向市場報告」的口徑，也就是 adjusted。
        原 Colab 已經在用，只是當第三順位；這裡維持同樣的位置。"""
        js = AV._get("EARNINGS", ticker)
        if not js:
            return empty(EPS_COLS)
        rows = []
        for it in js.get("quarterlyEarnings", [])[:n]:
            d = it.get("fiscalDateEnding", "")[:10]
            e = _f(it.get("reportedEPS"))
            if d and not np.isnan(e):
                rows.append(dict(period_end=pd.to_datetime(d).date(), eps=e))
        return pd.DataFrame(rows, columns=EPS_COLS)


# ───────────────────────── ④ SEC EDGAR ─────────────────────────
class SEC(Source):
    """免金鑰、最權威，但只有 GAAP 而且最慢，所以擺最後。"""
    name = "sec"
    needs_key = False
    HEADERS = {"User-Agent": os.getenv("SEC_USER_AGENT",
                                       "stock-tracker/1.0 (contact@example.com)"),
               "Accept-Encoding": "gzip, deflate"}
    _map = None
    _facts = {}

    @staticmethod
    def _cik(ticker):
        if SEC._map is None:
            try:
                js = requests.get("https://www.sec.gov/files/company_tickers.json",
                                  headers=SEC.HEADERS, timeout=30).json()
                SEC._map = {v["ticker"].upper(): str(v["cik_str"]).zfill(10)
                            for v in js.values()}
            except Exception:
                SEC._map = {}
        u = ticker.upper()
        return SEC._map.get(u.replace("-", ".")) or SEC._map.get(u)

    @staticmethod
    def _companyfacts(ticker):
        if ticker in SEC._facts:
            return SEC._facts[ticker]
        cik = SEC._cik(ticker)
        js = None
        if cik:
            try:
                js = requests.get(
                    f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
                    headers=SEC.HEADERS, timeout=45).json()
            except Exception:
                js = None
        SEC._facts[ticker] = js
        return js

    @staticmethod
    def _pull(js, tags, unit_pred, lo_days, hi_days):
        """★ 這裡就是原 Colab 那個 bug 的修正處 ★
        原本以 it["fy"]（報告的財年）為 key —— 一份 10-K 含三年數字、三筆 fy 相同，
        迴圈會一路覆蓋，留下哪一年看順序決定。
        改用每筆事實自己的 start / end 判斷期間，以 end 當 key；
        同期間多筆時取 filed 最新的，等於自動採用重編後的值。"""
        out = {}
        facts = ((js or {}).get("facts") or {}).get("us-gaap") or {}
        for tag in tags:
            for unit, arr in ((facts.get(tag) or {}).get("units") or {}).items():
                if not unit_pred(unit):
                    continue
                for it in arr:
                    end, val = it.get("end"), it.get("val")
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
                    prev = out.get(d)
                    if prev is None or it.get("filed", "") >= prev[1]:
                        out[d] = (float(val), it.get("filed", ""))
        return {k: v[0] for k, v in out.items()}

    @staticmethod
    def quarterly_financials(ticker, n=24):
        js = SEC._companyfacts(ticker)
        if not js:
            return empty(FIN_COLS)
        usd = lambda u: "usd" in u.lower() and "share" not in u.lower()
        shr = lambda u: "share" in u.lower()
        rev = SEC._pull(js, ["RevenueFromContractWithCustomerExcludingAssessedTax",
                             "Revenues", "SalesRevenueNet"], usd, 80, 100)
        gp = SEC._pull(js, ["GrossProfit"], usd, 80, 100)
        sh = SEC._pull(js, ["WeightedAverageNumberOfDilutedSharesOutstanding"], shr, 80, 100)
        eq = SEC._pull(js, ["StockholdersEquity",
                            "StockholdersEquityIncludingPortionAttributable"
                            "ToNoncontrollingInterest"], usd, None, None)
        m = YF.fy_end_month(ticker)
        rows = []
        for d in sorted(rev, reverse=True)[:n]:
            fy, fq = fiscal_from_period_end(d, m)
            rows.append(dict(period_end=d, fy=fy, fq=fq,
                             revenue=rev.get(d, np.nan), gross_profit=gp.get(d, np.nan),
                             shares_diluted=sh.get(d, np.nan),
                             total_equity=eq.get(d, np.nan)))
        return pd.DataFrame(rows, columns=FIN_COLS)

    @staticmethod
    def quarterly_eps_gaap(ticker, n=24):
        js = SEC._companyfacts(ticker)
        if not js:
            return empty(EPS_COLS)
        per_share = lambda u: "share" in u.lower() and "usd" in u.lower()
        eps = SEC._pull(js, ["EarningsPerShareDiluted", "EarningsPerShareBasic"],
                        per_share, 80, 100)
        rows = [dict(period_end=d, eps=eps[d]) for d in sorted(eps, reverse=True)[:n]]
        return pd.DataFrame(rows, columns=EPS_COLS)


REGISTRY = {"yf": YF, "yq": YQ, "av": AV, "sec": SEC}


def chain(names):
    return [REGISTRY[n] for n in names if n in REGISTRY]

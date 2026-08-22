"""所有來源共用的資料契約。

每個來源不必全部實作 —— 沒有的能力回傳空的 DataFrame / None，
sync.py 會自動往下一層找。這就是既有 Colab 那套瀑布的正式版。
"""
import pandas as pd

# 季度財報：一列一個財季
FIN_COLS = ["period_end", "fy", "fq", "revenue", "gross_profit",
            "shares_diluted", "total_equity"]
# 街頭口徑實際 EPS：一列一個財季
EPS_COLS = ["period_end", "eps"]


def empty(cols):
    return pd.DataFrame(columns=cols)


class Source:
    name = "base"
    needs_key = False

    @staticmethod
    def quarterly_financials(ticker: str, n: int) -> pd.DataFrame:
        """回傳 FIN_COLS。抓不到就回空表。"""
        return empty(FIN_COLS)

    @staticmethod
    def quarterly_eps_street(ticker: str, n: int) -> pd.DataFrame:
        """街頭口徑（adjusted / reported-to-street）的實際 EPS。回傳 EPS_COLS。"""
        return empty(EPS_COLS)

    @staticmethod
    def quarterly_eps_gaap(ticker: str, n: int) -> pd.DataFrame:
        """GAAP 稀釋 EPS。只在 street 全部失敗時使用，且會被標記 eps_basis='gaap'。"""
        return empty(EPS_COLS)

    @staticmethod
    def dividends(ticker: str, since) -> pd.Series:
        """index=除息日, value=每股配息。sync 會依季別加總成 dps。"""
        return pd.Series(dtype="float64")

    @staticmethod
    def price_history(ticker: str, since) -> pd.Series:
        """index=日期, value=收盤價。sync 用它取每個季末的價格。"""
        return pd.Series(dtype="float64")

    @staticmethod
    def estimates(ticker: str) -> dict:
        """{eps_f1, eps_f2, fy1_end, n_analysts, eps_q0}。抓不到的鍵留空。"""
        return {}

    @staticmethod
    def next_earnings(ticker: str):
        """下一次財報日（date）或 None。"""
        return None


def symbol_candidates(raw: str):
    """代號變體 —— 沿用既有 Colab 的處理（BRK.B / BRK-B / GOOG / GOOGL）。"""
    out, seen = [], set()

    def add(x):
        if x and x not in seen:
            out.append(x); seen.add(x)

    add(raw)
    add(raw.replace("-", "."))
    add(raw.replace(".", "-"))
    u = raw.upper()
    if u in ("GOOG", "GOOGL"):
        add("GOOG"); add("GOOGL")
    if u.replace("-", ".") in ("BRK.B", "BRK.A"):
        add("BRK-B"); add("BRK.B"); add("BRK-A"); add("BRK.A")
    return out


def fiscal_from_period_end(period_end, fy_end_month: int):
    """由期末日推出 (fy, fq)。

    多數來源不直接給公司自己的財年標號，只給期末日 —— 這是 Colab 版沒處理、
    導致 NVDA 這種一月結帳的公司會整個錯開一年的地方。

    規則：財年結束月份為 M 的公司，期末日落在 M 之後就屬於下一個財年。
    """
    y, m = period_end.year, period_end.month
    # 距離財年結束還有幾個月（0 = 這一季就是 Q4）
    delta = (fy_end_month - m) % 12
    fq = 4 - (delta // 3)
    fq = 4 if fq == 0 else fq
    fy = y + 1 if m > fy_end_month else y
    # 一月結帳（NVDA）：期末日 2026-01 屬於 FY2026
    if fy_end_month <= 3 and m > fy_end_month:
        fy = y + 1
    return int(fy), int(fq)

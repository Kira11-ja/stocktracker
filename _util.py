import re
import pandas as pd
import numpy as np


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def find_row(df, candidates):
    """在 DataFrame 的 index 裡找出候選欄名之一 —— 沿用既有 Colab 的模糊比對。
    yfinance 的欄名會隨版本變動（Total Revenue / Revenue / Operating Revenue），
    這段是原本 code 裡寫得最對的部分，原封保留。"""
    if df is None or len(df) == 0:
        return None
    m = {norm(i): i for i in df.index}
    for c in candidates:
        k = norm(c)
        if k in m:
            return m[k]
    for c in candidates:
        k = norm(c)
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
          "Total Equity Gross Minority Interest", "Total Equity",
          "Common Stock Equity"]
EPS_DIL = ["Diluted EPS", "Earnings Per Share Diluted", "DilutedEPS"]
EPS_BAS = ["Basic EPS", "Earnings Per Share Basic"]


def to_dt_cols(df):
    """把財報表的欄位轉成 datetime 並由新到舊排序。"""
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


def series_of(df, cands):
    r = find_row(df, cands)
    if r is None:
        return None
    return pd.to_numeric(df.loc[r], errors="coerce")

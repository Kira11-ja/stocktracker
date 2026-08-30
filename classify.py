#!/usr/bin/env python3
"""自動補上公司名稱與產業分類。

資料來源是 yfinance 的 info（yahooquery 當備援），它給的是英文的
GICS 風格字串，例如 Semiconductors、Software - Infrastructure、
Consumer Electronics。這裡用關鍵字規則對回中文分類。

兩個原則：
1. 只填「空白」或「未分類」的欄位 —— 你自己設過的一律不動。
   自動分類是省事用的，不是拿來覆蓋你的判斷。
2. 對不到就留「未分類」，不硬猜。對不到的原文會印在紀錄裡，
   你看了可以自己決定要歸到哪一類，或把它加進下面的 RULES。

用法：
    python classify.py            # 只補空的
    python classify.py --force    # 全部重新分類（會蓋掉你手動設的，慎用）
"""
import argparse
import csv
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent
DATA = ROOT / "data"
TICKERS = ROOT / "tickers.csv"
BLANK = ("", "未分類", "nan", "None")

# 依序比對，第一個命中的就是答案。順序有意義：
#   · 「Semiconductor Equipment」要排在「Semiconductor」前面，否則永遠對到後者。
#   · 房地產排最前面，因為 yfinance 的值長這樣：「REIT - Industrial」、
#     「REIT - Retail」、「REIT - Healthcare」，排後面會先被工業／零售／醫療吃掉。
RULES = [
    ("房地產", ["reit", "real estate"]),
    ("半導體設備", ["semiconductor equipment", "semiconductor manufacturing equipment"]),
    ("半導體", ["semiconductor"]),
    ("軟體", ["software"]),
    ("IT 服務", ["information technology services", "it services"]),
    ("資訊科技硬體", ["consumer electronics", "computer hardware",
                      "communication equipment"]),
    ("電子設備與零組件", ["electronic components", "electronics & computer distribution",
                          "scientific & technical instruments", "solar"]),
    ("電商", ["internet retail"]),
    ("網路服務", ["internet content", "internet & direct marketing"]),
    ("媒體與娛樂", ["entertainment", "broadcasting", "advertising", "publishing",
                    "electronic gaming"]),
    ("電信", ["telecom"]),
    ("量販與超市", ["discount stores", "grocery stores", "food distribution"]),
    ("零售通路", ["retail", "department stores"]),
    ("汽車", ["auto manufacturers", "auto parts", "auto & truck"]),
    ("餐旅休閒", ["restaurants", "lodging", "resorts", "casinos", "travel",
                  "leisure", "gambling"]),
    ("食品飲料菸草", ["beverages", "tobacco", "packaged foods", "confectioners",
                      "farm products"]),
    ("家庭與個人用品", ["household", "personal products", "personal care"]),
    ("銀行", ["banks", "mortgage finance", "savings"]),
    ("保險", ["insurance"]),
    ("資本市場", ["capital markets", "asset management",
                  "financial data & stock exchanges", "shell companies"]),
    ("金融科技與支付", ["credit services", "financial conglomerates"]),
    ("生技製藥", ["biotechnology", "drug manufacturers", "pharmaceutical"]),
    ("醫療設備", ["medical devices", "medical instruments", "diagnostics & research",
                  "medical appliances"]),
    ("醫療服務與保險", ["healthcare plans", "medical care", "health information",
                        "medical distribution", "pharmaceutical retailers"]),
    ("航太國防", ["aerospace", "defense"]),
    ("運輸", ["railroads", "airlines", "airports", "trucking", "freight",
              "marine shipping", "integrated freight"]),
    ("商業服務", ["business services", "consulting", "staffing", "security & protection",
                  "rental & leasing", "waste management"]),
    ("工業機械", ["machinery", "industrial", "building products", "engineering",
                  "electrical equipment", "tools & accessories", "conglomerates",
                  "manufacturing"]),
    ("能源", ["oil & gas", "uranium", "coal", "energy"]),
    ("公用事業", ["utilities"]),
    ("原物料", ["chemicals", "steel", "copper", "gold", "silver", "aluminum",
                "paper", "lumber", "building materials", "agricultural inputs",
                "other precious metals", "coking"]),
]


def _hit(key, text):
    """照單字邊界比對，不能直接用 in。
    「Credit Services」裡面含有 it services 這四個字，
    純用 in 會把信用卡公司分到 IT 服務去。
    結尾允許一個 s，因為 yfinance 單複數混用（Semiconductor / Semiconductors）。"""
    return re.search(r"(?<![a-z])" + re.escape(key) + r"s?(?![a-z])", text) is not None


def to_chinese(industry, sector):
    """先用細的 industry 對，對不到再用粗的 sector 撈一次。"""
    for text in (industry, sector):
        low = str(text or "").strip().lower()
        if not low:
            continue
        for zh, keys in RULES:
            if any(_hit(k, low) for k in keys):
                return zh
    return None


def is_blank(v):
    return str(v).strip() in BLANK or pd.isna(v)


def fetch(ticker):
    """回傳 (公司名, industry, sector, 來源)。抓不到就整組 None，不讓流程掛掉。"""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info or {}
        name = info.get("longName") or info.get("shortName")
        if name or info.get("industry"):
            return name, info.get("industry"), info.get("sector"), "yfinance"
    except Exception as e:
        print(f"      ! yfinance {ticker}: {e}")
    try:
        from yahooquery import Ticker
        d = Ticker(ticker).asset_profile
        p = d.get(ticker) if isinstance(d, dict) else None
        if isinstance(p, dict):
            q = Ticker(ticker).quote_type
            qq = q.get(ticker) if isinstance(q, dict) else {}
            name = (qq or {}).get("longName")
            return name, p.get("industry"), p.get("sector"), "yahooquery"
    except Exception as e:
        print(f"      ! yahooquery {ticker}: {e}")
    return None, None, None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="連已經填好的也重新分類（會蓋掉手動設定）")
    args = ap.parse_args()

    if not TICKERS.exists():
        print("找不到 tickers.csv")
        return 0
    df = pd.read_csv(TICKERS)
    for c in ("company", "sector"):
        if c not in df.columns:
            df[c] = ""

    changed, unmapped = 0, []
    for i, r in df.iterrows():
        t = str(r.get("ticker", "")).strip().upper()
        if not t:
            continue
        need_name = args.force or is_blank(r.get("company"))
        need_sector = args.force or is_blank(r.get("sector"))
        if not (need_name or need_sector):
            continue

        name, industry, sector, src = fetch(t)
        if src is None:
            print(f"  {t:<6} 抓不到公司資料，跳過")
            continue

        if need_name and name:
            df.at[i, "company"] = str(name)[:40]
            changed += 1
        if need_sector:
            zh = to_chinese(industry, sector)
            if zh:
                df.at[i, "sector"] = zh
                changed += 1
                print(f"  {t:<6} {industry or sector} → {zh}（{src}）")
            else:
                df.at[i, "sector"] = "未分類"
                unmapped.append(f"{t}: {industry or sector or '沒有資料'}")

    if unmapped:
        print("\n  以下對不到中文分類，先留「未分類」，可以在網頁上自己挑：")
        for u in unmapped:
            print(f"    {u}")

    if changed:
        df.to_csv(TICKERS, index=False, quoting=csv.QUOTE_MINIMAL)
        DATA.mkdir(exist_ok=True)
        df.to_csv(DATA / "tickers.csv", index=False, quoting=csv.QUOTE_MINIMAL)
        print(f"\n✓ 補了 {changed} 個欄位")
    else:
        print("✓ 沒有需要補的欄位")
    return 0


if __name__ == "__main__":
    sys.exit(main())

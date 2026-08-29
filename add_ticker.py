#!/usr/bin/env python3
"""從 GitHub Issue 新增／移除股票。

網頁上的「＋新增股票」會開一張標題是 `add: NVDA` 的 Issue，
這支程式負責把它變成 tickers.csv 的一列。移除則是 `remove: NVDA`。

刻意直接讀 GitHub 給的事件 JSON，不從 workflow 把標題內插進 shell——
Issue 標題是任何人都能打的字串，內插進 shell 等於開後門。
代號本身也會用白名單正規表達式驗過才使用。
"""
import csv
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent
DATA = ROOT / "data"
TICKERS = ROOT / "tickers.csv"
COLS = ["ticker", "公司名稱", "產業", "等級", "加入日期", "備註"]
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
TIERS = {"核心", "觀察", "池子"}


def emit(**kv):
    """把結果交給後面的 workflow 步驟。

    值一定要壓成單行再寫進 GITHUB_ENV —— 帶換行的值可以偽造出額外的環境變數，
    而這裡的內容有一部分來自 Issue 標題（誰都能打）。
    """
    kv = {k: re.sub(r"[\r\n]+", " ", str(v))[:200] for k, v in kv.items()}
    path = os.getenv("GITHUB_ENV")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")
    for k, v in kv.items():
        print(f"{k}={v}")


def parse_body(body):
    out = {}
    for line in (body or "").splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip().lower()
        if k in ("tier", "name", "note", "industry"):
            out[k] = v.strip()
    return out


def load_tickers():
    if TICKERS.exists():
        df = pd.read_csv(TICKERS)
    else:
        df = pd.DataFrame(columns=COLS)
    for c in COLS:
        if c not in df.columns:
            df[c] = ""
    return df


def strip_from_data(t):
    """移除時，把該檔從所有產出檔一起清掉，不然網頁還會看到它。"""
    for name in ("master.csv", "meta.csv", "raw_q.csv", "raw_est.csv", "raw_price.csv"):
        p = DATA / name
        if not p.exists():
            continue
        df = pd.read_csv(p)
        if "ticker" in df.columns:
            df[df.ticker != t].to_csv(p, index=False)


def main():
    ev_path = os.getenv("GITHUB_EVENT_PATH")
    if not ev_path or not Path(ev_path).exists():
        print("找不到 GitHub 事件檔，這支程式只能在 Actions 裡跑")
        emit(ACTION="none", TICKER="", RESULT="")
        return 0

    ev = json.loads(Path(ev_path).read_text(encoding="utf-8"))
    issue = ev.get("issue") or {}
    title = (issue.get("title") or "").strip()
    body = issue.get("body") or ""

    m = re.match(r"^\s*(add|remove)\s*[:：]\s*(.+)$", title, re.I)
    if not m:
        emit(ACTION="none", TICKER="", RESULT="標題不是 add: / remove: 開頭，略過")
        return 0

    action = m.group(1).lower()
    t = m.group(2).strip().upper()
    if not SYMBOL.match(t):
        emit(ACTION="none", TICKER="",
             RESULT=f"「{t[:20]}」看起來不像美股代號，沒有做任何事")
        return 0

    df = load_tickers()
    have = df.ticker.astype(str).str.upper().tolist()

    if action == "add":
        if t in have:
            emit(ACTION="none", TICKER=t, RESULT=f"{t} 已經在清單裡了，沒有重複加入")
            return 0
        f = parse_body(body)
        tier = f.get("tier", "")
        row = {
            "ticker": t,
            "公司名稱": f.get("name", "") or "",
            "產業": f.get("industry", "") or "",
            "等級": tier if tier in TIERS else "池子",
            "加入日期": dt.date.today().isoformat(),
            "備註": f.get("note", "") or "",
        }
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
        df.to_csv(TICKERS, index=False, quoting=csv.QUOTE_MINIMAL)
        emit(ACTION="add", TICKER=t,
             RESULT=f"已加入 {t}（等級 {row['等級']}），正在抓歷史資料")
        return 0

    if t not in have:
        emit(ACTION="none", TICKER=t, RESULT=f"{t} 本來就不在清單裡")
        return 0
    df[df.ticker.astype(str).str.upper() != t].to_csv(
        TICKERS, index=False, quoting=csv.QUOTE_MINIMAL)
    strip_from_data(t)
    emit(ACTION="remove", TICKER=t, RESULT=f"已移除 {t} 與它的歷史資料")
    return 0


if __name__ == "__main__":
    sys.exit(main())

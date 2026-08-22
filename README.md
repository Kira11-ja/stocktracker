# 美股觀察表 · 資料管線

每個交易日自動更新，只抓缺的資料，抓完驗證過才寫回資料庫。

## 第一次設定（約 15 分鐘，只做一次）

1. **建 repo**：把這個資料夾推上 GitHub。財報數字是公開資訊，設 public 最省事
   （Excel 直接讀 raw URL，不用處理權限）。
2. **設 Secrets**（Settings → Secrets and variables → Actions）：
   - `SEC_USER_AGENT` — SEC 規定要帶聯絡方式，例如 `stock-tracker/1.0 (你的信箱)`
   - `AV_API_KEY` — Alpha Vantage 金鑰。**可以不設**，只是少一層備援
3. **第一次跑**：Actions 分頁 → `update-stock-data` → Run workflow。
   5 檔大約 3~6 分鐘（要抓 24 季歷史）。
4. **接到 Excel**：見下方。

## 加一檔股票

編輯 `tickers.csv`（可以直接在 GitHub 網頁上改），加一行後存檔。
下次自動執行時就會抓；不想等就到 Actions 按 Run workflow。

`sync.py` 會發現這檔不在 master 裡 → 自動抓滿 24 季歷史 + 季末股價 + 預估。
**不需要為新標的做任何特別處理**，「第一次全補」只是「缺口 = 全部」的特例。

## 每天實際會發生什麼

| 情況 | 判斷 | 動作 |
|---|---|---|
| 新標的 | 不在 master | 抓滿 24 季 |
| 季數不足 | `< target_quarters` | 只補缺的 |
| 財報日未到 | `today <= next_earnings` | **季度 API 全跳過**，只更新股價 |
| 財報公布了 | `today > next_earnings` | 抓最近幾季，預估列翻成實際，新增下一季預估 |

多數日子多數股票走第三條，所以平常的 API 用量很低。

## 資料來源瀑布

沿用原本 Colab 的分層，後面的來源只補前面缺的期別，不覆寫已有的值。

```
財報三表   yfinance → yahooquery → Alpha Vantage → SEC EDGAR
街頭 EPS   yfinance(earnings_dates) → yahooquery(earnings_history) → AV(EARNINGS)
預估       yahooquery(earnings_trend) → yfinance(info.forwardEps)
股價/股利  yfinance
```

**兩條規則**（這是跟原本 Colab 最大的差別）：

1. **只在同口徑內遞補。** 街頭口徑（adjusted）之間可以互補；街頭全部失敗才退回
   GAAP，而且會在 `eps_basis` 欄寫上 `gaap`。哪一格是降級來的一眼看得到，
   PE / PEG 不會被靜默污染。
2. **驗證在寫回 master 之前。** 四項檢查（重複鍵、營收非正、季度斷層、GAAP 降級）
   任一不過就 `exit(1)`，Actions 標紅寄信，master 保持乾淨。

## 接到 Excel

在 Excel 裡：**資料 → 取得資料 → 從其他來源 → 從 Web**，貼上：

```
https://raw.githubusercontent.com/<你的帳號>/<repo>/main/data/raw_q.csv
```

`raw_est.csv`、`raw_price.csv` 同樣做一次。之後每次按「全部重新整理」就是最新資料。

Excel 裡的 `seq` 欄是公式不是資料 —— 新一季補進來時它會自己重排，
TTM / YoY / QoQ / 加速度全部自動往前滾一格，你不用碰任何公式。

## 本機除錯

```bash
pip install -r requirements.txt
python sync.py --dry-run              # 只印出每檔會走哪條分支，不抓也不寫
python sync.py --only AAPL            # 只跑一檔
python sync.py --force-full           # 忽略快取全部重抓
```

## 已知待辦

- 各來源的欄位名稱會隨套件版本變動。第一次跑請看 log 裡每檔的
  `財報=... EPS=...(basis)`，確認走的是預期的來源。
- `estimates()` 目前沒填 `fy1_end`（EPS_NTM 的加權需要它）。
  yahooquery 的 earnings_trend 有 `endDate`，接上去即可。

# 美股觀察表 · 資料管線

每個交易日自動更新，只抓缺的資料，驗證過才寫回資料庫。

**這個 repo 刻意做成扁平結構**（除了 `.github/workflows/` 那個 GitHub 規定的路徑），
所有 .py 都在根目錄，用 GitHub 網頁上傳時不會有資料夾被壓平的問題。

## 檔案

| 檔案 | 作用 |
|---|---|
| `tickers.csv` | **你唯一要動的檔案**。加股票就在這裡加一行 |
| `config.yaml` | 參數：季數、重編視窗、來源順序 |
| `sync.py` | 增量邏輯 + 驗證 + 輸出 |
| `sources.py` | 四層資料來源瀑布 |
| `.github/workflows/update.yml` | 排程（每個交易日台灣時間 07:00） |
| `data/` | 程式自動建立，放 master 與輸出的 csv |

## 第一次設定

1. **上傳檔案**：根目錄放 `tickers.csv` / `config.yaml` / `sync.py` / `sources.py` /
   `requirements.txt` / `README.md`。
   `update.yml` 要用 **Add file → Create new file**，檔名打
   `.github/workflows/update.yml`（打斜線會自動變資料夾），內容貼進去。
2. **開寫入權限**：Settings → Actions → General → Workflow permissions →
   **Read and write permissions** → Save。（不開的話最後 commit 那步會失敗）
3. **設 Secrets**：Settings → Secrets and variables → Actions
   - `SEC_USER_AGENT` = `stock-tracker/1.0 (你的信箱)` — SEC 規定要帶聯絡方式
   - `AV_API_KEY` = Alpha Vantage 金鑰 — **可以不設**，只是少一層備援
4. **第一次跑**：Actions → `update-stock-data` → Run workflow。5 檔約 3~6 分鐘。

## 加一檔股票

編輯 `tickers.csv`（可以直接在 GitHub 網頁上點鉛筆圖示改），加一行存檔。
下次自動執行就會抓；不想等就去 Actions 按 Run workflow。

程式會發現這檔不在 master 裡 → 自動抓滿 24 季歷史 + 季末股價 + 預估。
不需要任何特別處理：「第一次全補」只是「缺口 = 全部」的特例，跟增量共用同一段程式碼。

## 每天實際會發生什麼

| 情況 | 判斷 | 動作 |
|---|---|---|
| 新標的 | 不在 master | 抓滿 24 季 |
| 季數不足 | `< target_quarters` | 只補缺的 |
| 財報日未到 | `today <= next_earnings` | **季度 API 全跳過**，只更新股價 |
| 財報公布了 | `today > next_earnings` | 抓最近幾季；預估列翻成實際，並新增下一季預估 |

多數日子多數股票走第三條，所以平常 API 用量很低。

## 資料來源瀑布

```
財報三表   yfinance → yahooquery → Alpha Vantage → SEC EDGAR
街頭 EPS   yfinance(earnings_dates) → yahooquery(earnings_history) → AV(EARNINGS)
預估       yahooquery(earnings_trend) → yfinance(info.forwardEps)
股價/股利  yfinance
```

**兩條規則**（跟原本 Colab 最大的差別）：

1. **只在同口徑內遞補。** 街頭口徑（adjusted）之間可以互補；街頭全失敗才退回 GAAP，
   而且會在 `eps_basis` 欄寫上 `gaap`。哪一格是降級來的一眼看得到。
2. **驗證在寫回 master 之前。** 重複鍵、營收非正、季度斷層、GAAP 降級四項檢查，
   任一不過就 `exit(1)`，Actions 標紅寄信，master 保持乾淨。

## 接到 Excel

Excel → **資料 → 取得資料 → 從其他來源 → 從 Web**，貼上：

```
https://raw.githubusercontent.com/Kira11-ja/stocktracker/main/data/raw_q.csv
```

`raw_est.csv`、`raw_price.csv` 各做一次。之後按「全部重新整理」就是最新資料。

Excel 裡的 `seq` 欄是公式不是資料 —— 新一季補進來時它會自己重排，
TTM / YoY / QoQ / 加速度 / 動能判讀全部自動往前滾一格。

## 本機除錯

```bash
pip install -r requirements.txt
python sync.py --dry-run     # 只印每檔會走哪條分支，不抓也不寫
python sync.py --only AAPL   # 只跑一檔
python sync.py --force-full  # 忽略快取全部重抓
```

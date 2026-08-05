# monitor_taipower_curve

台電「今日用電曲線」中繼爬蟲——**必須跑在一般網路的機器上（例如辦公室的 Mac），不能跑在雲端主機。**

## 為什麼要一台獨立機器

台電官網 `www.taipower.com.tw` 的 CloudFront **對整台主機封鎖雲端 ASN**。
2026-08-05 實測：

| 來源 | 結果 |
|---|---|
| GCP 東京（analyzer-2） | HTTP 403，連首頁都 403 |
| GCP 台灣（crawler-2，asia-east1） | HTTP 403 |
| 一般網路（家用／辦公室） | **HTTP 200** |

補齊完整瀏覽器標頭、改 HTTP/2 都無效——是 IP／ASN 層級的封鎖，不是請求特徵問題。
另外也確認過：

- 開放資料平台 `service.taipower.com.tw` **沒有**用電曲線資料集
  （實測 d006002~d006006、d006021 全部 404）
- 沒有其他台電主機提供同一份檔（`service.` 回 404、`data.`／`open.` 不存在）
- CSV **沒有 CORS 標頭**，所以前端直接抓會被瀏覽器擋

所以只剩「從一般網路的機器抓，再寫進資料庫」這條路。這個專案就是那台機器要跑的東西。

## 要抓什麼

三支 CSV，10 分鐘一點、**當日累積**：

| 檔案 | 內容 |
|---|---|
| `.../loadGraph/data/loadfueltype.csv` | 今日用電曲線圖－依燃料類別（12 欄）|
| `.../loadGraph/data/loadareas.csv` | 今日用電曲線圖－依區域別（4 欄）|
| `.../loadGraph/data/genloadareaperc.csv` | 各區發電／用電占比（上方那排長條）|

Base：`https://www.taipower.com.tw/d006/loadGraph/loadGraph/data/`

**★ 因為檔案是「當日累積」，每小時抓一次就能拿到全部 144 個 10 分鐘點——
不需要每 10 分鐘跑。** 這是這個設計最重要的一點。

## 快速開始

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp config/config.yml.example config/config.yml   # 填密碼
# 把三個憑證放進 config/ssl/（見下方）
./venv/bin/python scripts/preflight.py           # 1. 這台機器抓不抓得到？（不碰資料庫）
python3 scripts/verify_fixtures.py               # 2. 欄位對應驗收（用內附 fixture，零相依）
python3 scripts/verify_fixtures.py --live        #    同上但抓即時資料
```

前兩支**實作完成前就能跑**。`verify_fixtures.py` 會印出兩支曲線的總和並比對——
差 50 MW 以內才算欄位對應正確（實測差 1 MW）。這就是驗收條件。

## 憑證

Cloud SQL 用 client certificate。跟 `monitor_strait_info` 是同一組，
從既有機器複製過來，放在：

```
config/ssl/server-ca.pem
config/ssl/client-cert.pem
config/ssl/client-key.pem      # 權限要 600
```

## 排程（macOS 用 launchd，不是 cron）

`deployment/tw.nics.taipower-curve.plist` 是範本，安裝方式見 `docs/DEPLOY.md`。

## 怎麼交到那台 Mac

這個目錄本身就是一個獨立 git repo（已 `git init` 並提交）。任選一種：

- `git bundle create tpc.bundle --all` 後把 bundle 檔帶過去 `git clone tpc.bundle`
- 或直接整個目錄複製過去（**記得排除 `config/config.yml` 與 `config/ssl/*.pem`**，
  那兩者含密碼與私鑰，本來就在 .gitignore 裡）

到那台機器後：先跑 `scripts/preflight.py`，再讀 `CLAUDE.md` 開始實作。

## 這個專案**不做**什麼

- 不做視覺化：畫面在 dashboard-app，這裡只負責把資料寫進去
- 不做 schema migration：表由 dashboard-app 的 `alembic_monitor` 建立與管理
  （見 `docs/SCHEMA.md`），這裡只 upsert
- 不重試到天荒地老：抓不到就記一次失敗並推遙測，讓監控看得見

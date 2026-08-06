# monitor_taipower_curve

台電「今日用電曲線」中繼爬蟲——**必須跑在一般網路的機器上（例如辦公室的 Mac），
不能跑在雲端主機。**

**現況**：2026-08-05 上線，launchd 每小時跑一次，寫入 Cloud SQL。
運維與排錯看「抓取健康度」與「確認它真的在做事」兩節。

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

- 開放資料平台 `service.taipower.com.tw` **沒有**這四支檔的任何欄位
  （2026-08-06 實測 d006002~d006008、d006021 全部 404；只有 d006001
  「各機組發電量」存在，內容不重疊）
- 沒有其他台電主機提供同一份檔（`data.`／`open.` 不存在）
- CSV **沒有 CORS 標頭**，所以前端直接抓會被瀏覽器擋

所以只剩「從一般網路的機器抓，再寫進資料庫」這條路。

## 寫入目標

Cloud SQL 的 **`strait_info_monitor_prod`**（2026-08-06 起；先前是 `dashboard_monitor`）。

★ 那是爬蟲庫，`monitor_*` 表由 `monitor_strait_info` 擁有。本專案是該庫的
**第二個寫入者，但只寫自己的兩張表**——`monitor_power_load_curve` 與
`monitor_fetch_run`。**一表一寫入者**是平台原則，不要跨線去寫別人的表。

- 帳號用 **dashboard**（不是 crawler），只在這兩張表有寫入權限
- dashboard-app 只負責**讀取顯示**，不寫
- 契約見 `docs/SCHEMA.md`

## 要抓什麼

四支檔，Base：`https://www.taipower.com.tw/d006/loadGraph/loadGraph/data/`

| 檔案 | 內容 | 累積性 |
|---|---|---|
| `loadfueltype.csv` | 今日用電曲線－依燃料類別（12 欄）| **當日累積**，一抓拿回整天 |
| `loadareas.csv` | 今日用電曲線－依區域別（4 欄）| **當日累積**，一抓拿回整天 |
| `genloadareaperc.csv` | 各區發電／用電占比 | **只有當下**，一次一點 |
| `loadpara.json` | 即時供電能力、即時用電、尖峰預估 | **只有當下**，一次一點 |

**★ 因為前兩支是「當日累積」，每小時抓一次就能拿到全部 144 個 10 分鐘點——
不需要每 10 分鐘跑。** 這是這個設計最重要的一點。

★ 但後兩支不累積，解析度就等於執行頻率（每小時一點），漏掉的永遠補不回來。

### loadpara.json：兩個「供電能力」不是同一件事

| 欄位 | 意義 | 會不會變 |
|---|---|---|
| `real_hr_maxi_sply_capacity` | **即時供電能力** | 每次抓都不同 |
| `fore_maxi_sply_capacity` | 今日**預估**最大供電能力 | 一天固定 |

★★ 台電網頁上那個「使用率 %」的分母是**即時供電能力**，不是今日最大供電能力。
拿錯分母會差約一個百分點（2026-08-06 實測：81% vs 82%）。
兩個值都存，`kind='capacity'`，見 `docs/SCHEMA.md`。

## 安裝與設定

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp config/config.yml.example config/config.yml   # 填密碼與 pushgateway
# 把三個憑證放進 config/ssl/（見下方）
./venv/bin/python scripts/preflight.py           # 這台機器抓不抓得到？（不碰資料庫）
./venv/bin/python scripts/verify_fixtures.py     # 欄位對應驗收（用內附 fixture，零相依）
./venv/bin/pytest -q                             # 全部測試
./venv/bin/python scripts/run_once.py            # 正式跑一次
```

`preflight.py` 與 `verify_fixtures.py` **不碰資料庫也不需要 config**，
所以是排錯時第一個該跑的兩支。`verify_fixtures.py` 會印出兩支曲線的總和並比對——
差 50 MW 以內才算欄位對應正確（實測差 1 MW）。

### 憑證

Cloud SQL 用 client certificate，跟 `monitor_strait_info` 同一組：

```
config/ssl/server-ca.pem
config/ssl/client-cert.pem
config/ssl/client-key.pem      # 權限要 600，否則 psycopg2 拒絕連線
```

★ 這台機器的**對外 IP** 要同時在三個白名單裡：Cloud SQL authorized networks、
Pushgateway 的防火牆規則。換網路（VPN、熱點、不同網段）就會掉出白名單。

### 排程（macOS 用 launchd，不是 cron）

`deployment/tw.nics.taipower-curve.plist` 是範本，安裝方式見 `docs/DEPLOY.md`。
★ 用 `StartCalendarInterval` 對齊時鐘（每小時 `:55` ＋ `23:59` 收尾），
**不可改回 `StartInterval`**——理由見 `CLAUDE.md`，會每天固定掉四個時間點。

## 原文歸檔

★ 平台紀律：**原文全存，解析錯了可以重跑。** 每次執行把四支檔的原始 bytes
存進 `data/raw/YYYY-MM-DD/HHMMSS/`，附一份 MANIFEST 記錄每支檔的
**來源網址、bytes 數、sha256**。`monitor_fetch_run.raw_uri` 指到那個目錄、
`raw_sha256` 是 MANIFEST 的雜湊（MANIFEST 裡有每支檔各自的雜湊，驗一份等於驗全部）。

- 歸檔在**解析之前**做——解析失敗才是最需要原文的時候。
- 保留 90 天，舊的自動清掉。`data/` 不進 git。
  實測一次約 25 KB（檔案隨當日累積變大），一天約 400 KB，90 天約 35 MB。
- 那些沒進資料庫的欄位（使用率、備轉容量率、燈號、發布時間、昨日摘要）
  **全部在原文裡**，需要時回去撈。

## 抓取健康度：漏幾次算正常？

★ **判斷標準不是「漏幾次」，是「漏在哪裡」**——因為兩支曲線是當日累積檔，
下一次成功的執行會把整天補回來。

| 情境 | 資料損失 |
|---|---|
| 漏掉 00:55–22:55 之間任何幾次 | **沒有損失**，下次成功就補回整天曲線 |
| 漏掉當天最後兩次（23:55、23:59）| 當天尾巴永久遺失（檔案 00:00 換日重置）|
| 整天都沒成功 | 該日曲線永久遺失 |
| 任何一次漏掉 | `area_gen` / `capacity` 少一個時間點，**永久補不回**（不累積）|

實務門檻：

- **存活告警**：`time() - scrapy_last_success_timestamp_seconds > 3 小時`
  （＝連續 3 次失敗）。已在監控端設好。
- **要查但先別緊張**：一天 25 次裡成功 ≥ 23 次。曲線是完整的，只是 `capacity` 有洞。
- **要動手**：連續兩天成功 < 20 次，或 23:55／23:59 那兩次連續失敗。

### 四種失敗長得很像，但要修的東西完全不同

| 症狀 | 原因 | 怎麼確認 |
|---|---|---|
| `fetch_run` 整段沒紀錄，遙測也沒更新 | 關機或睡眠 | `pmset -g` 看 sleep 有沒有被 caffeinate 擋住 |
| `status='error'`、訊息提到 CloudFront/HTML | 出口 IP 被台電擋 | `./venv/bin/python scripts/preflight.py` |
| `status='error'`、訊息說「資料庫端」 | IP 不在 Cloud SQL 白名單 | `curl -s https://api.ipify.org` 對照白名單 |
| 執行正常但遙測 WARNING 推不上去 | IP 不在 Pushgateway 防火牆白名單 | `curl -m 20 <pushgateway>/metrics` 要回 200 |

★ 判定「連不上」前**給足 20 秒並重測兩三次**，且分清逾時（路由／防火牆）
與 connection refused（服務沒起來）——2026-08-05 就因為只用 8 秒逾時測了一次，
把一個其實通的 Pushgateway 誤判成被防火牆擋，差點害人去改不用改的規則。

★ launchd 的補跑行為：機器睡著時錯過的排程，**喚醒後只補跑一次**，
不會把錯過的每一次都補。睡 8 小時只補 1 次——這台機器應設成不睡。
關機期間錯過的，則在下次登入載入 LaunchAgent 時跑一次（`RunAtLoad`）。

★ 失敗時遙測**確實會推**（2026-08-06 實測過資料庫連不上與程式未預期例外兩種情境）：
`scrapy_log_errors` 會 >0，而 `scrapy_last_success_timestamp_seconds`
**不會被更新也不會被洗掉**，所以存活告警算得出「多久沒成功了」。

## 確認它真的在做事

```sql
-- 今日各 kind 的涵蓋範圍。fuel 應是 12×N、area 4×N，N 隨當日時間長到 144
SELECT kind, count(*), count(DISTINCT observed_at) AS 時點數,
       max(observed_at AT TIME ZONE 'Asia/Taipei')::time AS 最新
FROM monitor_power_load_curve
WHERE (observed_at AT TIME ZONE 'Asia/Taipei')::date
      = (now() AT TIME ZONE 'Asia/Taipei')::date
GROUP BY 1 ORDER BY 1;

-- 曲線有沒有斷點（正常應為 0）
SELECT count(*) FROM (
  SELECT observed_at, lag(observed_at) OVER (ORDER BY observed_at) prev
  FROM (SELECT DISTINCT observed_at FROM monitor_power_load_curve
        WHERE kind='fuel' AND (observed_at AT TIME ZONE 'Asia/Taipei')::date
              = (now() AT TIME ZONE 'Asia/Taipei')::date) x) y
WHERE prev IS NOT NULL AND observed_at - prev > interval '10 minutes';

-- 今日每次執行的結果
SELECT (fetched_at AT TIME ZONE 'Asia/Taipei')::timestamp(0), status,
       record_count, duration_ms, note
FROM monitor_fetch_run WHERE source_id='taipower_loadcurve'
  AND (fetched_at AT TIME ZONE 'Asia/Taipei')::date
      = (now() AT TIME ZONE 'Asia/Taipei')::date
ORDER BY fetched_at;
```

日誌在 `/tmp/taipower-curve.log`。

## 這個專案**不做**什麼

- 不做視覺化：畫面在 dashboard-app，這裡只負責把資料寫進去
- 不做 schema migration：`monitor_*` 表由 `monitor_strait_info` 擁有與管理
  （見 `docs/SCHEMA.md`），這裡只 upsert 自己那兩張
- 不重試到天荒地老：抓不到就記一次失敗並推遙測，讓監控看得見
- 不往「規避封鎖」的方向做：擋的是 IP／ASN，調標頭沒用也不該試

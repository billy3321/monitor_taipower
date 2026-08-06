# CLAUDE.md — monitor_taipower_curve

給接手實作的 AI：這份是**規格與紀律**，不是建議。以下每一條都有踩過的實例。

## 這個專案要做的事

從一般網路的機器抓台電四支檔（三支 CSV ＋ `loadpara.json`），寫進 Cloud SQL 的
`strait_info_monitor_prod` 資料庫（2026-08-06 前是 `dashboard_monitor`），每小時一次。就這樣。
不要順手加視覺化、不要加 web 介面、不要加 ORM 以外的抽象層。

**DO NOT OVERDESIGN. DO NOT OVERENGINEER.**

## ★★ 欄位對應：這是本專案最容易錯的地方

兩支主要 CSV **都沒有表頭**。以下欄序是 2026-08-05 從
`https://www.taipower.com.tw/d006/loadGraph/loadGraph/load_fueltype_.html`
的**現行 JS 分支** balloon 文字逆向取得，並用另一份資料交叉驗證過。

### loadfueltype.csv（時間欄 + 12 個數值欄）

| 欄 | 名稱 |
|---|---|
| 1 | 燃氣 |
| 2 | 民營電廠-燃氣 |
| 3 | 燃煤 |
| 4 | 民營電廠-燃煤 |
| 5 | 汽電共生 |
| 6 | 重油 |
| 7 | 太陽能 |
| 8 | 風力 |
| 9 | 水力 |
| 10 | 儲能 |
| 11 | 其它再生能源 |
| 12 | 儲能負載（**負值，正常**）|

### loadareas.csv（時間欄 + 4 個數值欄）

| 欄 | 名稱 |
|---|---|
| 1 | 東部 |
| 2 | 南部 |
| 3 | 中部 |
| 4 | 北部 |

**★ 不是「東北中南」。** 我第一次照直覺猜東北中南是錯的，拿官網當日顯示的
數字對帳才發現：最後一欄 1434.2 對應「北部用電 1434.3」。這個順序也正是
圖上由下往上的堆疊順序。

### ★ 三個陷阱

1. **同一份 HTML 裡有兩組設定，舊的那組是錯的。**
   `if (AmCharts.recommended()!='js' || msie)` 分支裡的 `<graphs><title>` 是
   **Flash 舊版**，它還列著已除役的核能，導致整組欄位位移——照它對會全錯。
   要看 `else` 之後那段（現行 JS 分支）的 balloon 文字。

2. **單位是萬瓩，不是 MW。** 1 萬瓩 = 10 MW。寫進資料庫前一律換算成 MW，
   跟 `monitor_power_unit_observation` 同單位。同一張圖上兩種單位遲早出事。

3. **時間欄有兩種寫法**：`00:10` 與整點的 `00`，同一個檔裡都會出現。

4. **★ 檔案永遠是 144 列，未來時段預先留白。**
   2026-08-05 12:20 實測：144 列裡只有 74 列有值，其餘 70 列的內容是 `,`
   （只有一個逗號，連時間欄都空）。這不是檔案壞掉，是當日尚未發生的時段。

   **絕對不可以把它們寫成 0 MW**——那會讓今天下午的用電看起來是零。
   正確做法是**整列跳過不寫入**（欄位數不足就 skip），
   不是寫 NULL 也不是寫 0：那些時間點根本還沒到，連「未報告」都算不上。

   ★ 也因此，「這個檔有 144 列」不代表抓到完整一天。
     要判斷抓到多少，看的是**有值的列數**。

## 先跑這兩支（實作前）

```bash
python3 scripts/preflight.py         # 這台機器抓不抓得到台電官網
python3 scripts/verify_fixtures.py   # 欄位對應驗收，零相依，直接可跑
```

`verify_fixtures.py` 已經把下面說的交叉檢查實作好了，並附真實 fixture
（`tests/fixtures/`，2026-08-05 實抓）。**先跑過一次再開始寫**，
你會直接看到正確的數字長什麼樣。

## 驗收：怎麼確認欄序沒對錯

**兩支曲線是同一份用電的兩種切分，同一時點的總和必須吻合**（實測差 1 MW 以內）。
標錯或漏掉任何一欄，兩邊總和就會分岔。**單看某一欄「看起來合理」驗不出欄序錯置**，
一定要用這個交叉檢查，並寫成測試。

參考值（2026-08-05 10:00）：能源別合計 38,103 MW、區域別合計 38,102 MW。

## 紀律（家族共通，違反過的都在這裡）

### 未知 ≠ 零
- 空字串／`-`／`N/A` → `None`，**不是 0**。`'0.0'` 才是真的零。
- 資料庫欄位允許 NULL，並在 comment 寫明「NULL=未報告，0=真的零」。
- 抓不到整份檔案時**不要**寫入零列或部分列——那會讓圖上出現假的低谷。

### 儲能負載是負的，那是正常的
不要濾掉、不要取絕對值。那是充電側，離峰儲電、尖峰放電。
台電官方也把它畫成負值並計入合計。

### 重疊要 upsert 不要重複寫入
檔案是當日累積，每次抓都會涵蓋整天已發生的部分、跟上次大量重疊。
主鍵 `(observed_at, kind, label)`，`ON CONFLICT DO UPDATE`。
**台電事後會修同一個時間點的值**，所以是 UPDATE 不是 DO NOTHING。

### 靜默失敗要變成可見失敗
- 官網回 200 但內容是 CloudFront 403 HTML 是踩過的坑——**必驗內容**
  （檢查前幾百 bytes 有沒有 `<html`），只看狀態碼會放行。
- 欄數與預期不符要**丟例外**，不要猜著對——猜錯的代價是整張圖標籤錯位，
  而且畫面看起來完全正常，可能好幾天沒人發現。
- 每次執行都要推遙測（見下），失敗也要推。沒有遙測 = 爬蟲死了沒人知道。

### HTTP 用函式庫，標頭要與真實瀏覽器一致

- **用 `requests`（或 httpx），不要 shell out 去呼叫 curl。** curl 沒有錯誤型別、
  沒有連線重用、逾時與重試都要自己拼字串，而且 subprocess 的失敗很難分類。
  `preflight.py` 已經示範了要的形狀：Session + 自訂 Adapter。

- **標頭要送完整的一組**，不是只有 UA。理由不是「騙過誰」，而是
  **讓我們的請求跟那個頁面自己發的請求長得一樣**——WAF 常對
  「只有 UA、沒有 Accept/Accept-Language/Referer」的請求提高警覺，
  那種請求在真實瀏覽器裡根本不存在。建議這一組：

  ```python
  {
      'User-Agent': <config 的 crawler.user_agent>,
      'Accept': 'text/csv,text/plain,*/*',
      'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
      'Referer': 'https://www.taipower.com.tw/2289/2363/2367/2368/10264/normalPost',
      'Sec-Fetch-Dest': 'empty',
      'Sec-Fetch-Mode': 'cors',
      'Sec-Fetch-Site': 'same-origin',
  }
  ```

  Referer 用那個頁面本來就會載入這支 CSV 的網址（能源別是 10264、
  區域別是 10263），不要亂填。

- ★ **但不要往「規避封鎖」的方向做**。實測結論已經很清楚：CloudFront 擋的是
  IP／ASN，補標頭、換 HTTP/2 在被擋的機器上**一點用都沒有**（三台 GCP
  全試過）。在允許的網路上，標頭只是讓請求正常；在被擋的網路上，
  再怎麼調標頭都沒用，也不該去試。抓不到就讓它失敗、讓告警響。

- 保留既有的 `RelaxedGovTwAdapter`：台灣政府 PKI 缺 Subject Key Identifier，
  Python 3.13 預設會拒絕。**絕不可改成 `verify=False`。**

- UA 字串從 `config.yml` 的 `crawler.user_agent` 讀，不要寫死在程式裡
  （家族慣例，方便 Chrome 版本老舊時統一調整）。

### 爬取自律
每次執行只打 **4** 個請求（2026-08-06 起多抓 `loadpara.json`）、每小時一次。
不要加重試風暴。兩個請求之間 sleep 一下。

### ★ 原文全存，解析錯了可以重跑

平台紀律。每次執行把四支檔的原始 bytes 存進 `data/raw/YYYY-MM-DD/HHMMSS/`，
附 MANIFEST 記錄來源網址／bytes／sha256，保留 90 天。見 `taipower_curve/archive.py`。

★ **歸檔要在解析之前做。** 解析失敗才是最需要原文的時候——先存下來，
之後才有得重跑；反過來做的話，來源改版那天你會兩手空空。

★ 歸檔失敗**不該讓整次執行失敗**（記 WARNING 與一次 error 就好）：
資料進得了資料庫比留副本重要。

### ★ 空回應／挑戰頁不等於「今天沒資料」

`content-type` 與內容**都要驗**，只驗一個都會被騙：

- 只看狀態碼：CloudFront 擋頁是 HTTP 200 + HTML（踩過的坑）
- 只看 content-type：擋頁有時仍標成 `text/csv`
- 只看內容：content-type 明說是 HTML 就別再猜了

JSON 端點要真的 `json.loads` 得起來才算成功。見 `fetch._validate()`。

## 遙測（必做）

推到 Pushgateway，契約與家族一致：

- `job = monitor_taipower_curve`
- `grouping_key = {instance_id, spider}`，spider 用 `loadcurve`
- 指標：`scrapy_last_run_timestamp_seconds`（每次都設）、
  `scrapy_last_success_timestamp_seconds`（**僅成功時設**）、
  `scrapy_items_scraped`、`scrapy_log_errors`、`scrapy_run_duration_seconds`
- 全部是 gauge、單一 `scrapy_` 前綴、無 `_total`
- 推送失敗只 WARNING 不中斷爬蟲，timeout 5 秒
- URL 從 `config.yml` 的 `monitoring.pushgateway.url` 讀

★ 存活告警是 `time() - scrapy_last_success_timestamp_seconds > 門檻`，
**不可用 `up`**（Pushgateway 的 up 永遠是 1）。

### ★★ 一定要用 `pushadd_to_gateway`，不可用 `push_to_gateway`

`push_to_gateway()` 是 **HTTP PUT ＝ 整組取代**。而契約是
「`scrapy_last_success_timestamp_seconds` 僅在成功時設」——所以**失敗那次**
推上去的那組裡沒有它，PUT 會把**前一次成功的時間戳一併洗掉**。
序列不存在時，`time() - scrapy_last_success_timestamp_seconds > 門檻`
是對空向量求值，**永遠不會燒**。

也就是說：**爬蟲失敗這件事本身，會讓偵測它失敗的告警消失。**

不是理論——2026-08-05 家族的 `adsb` 從 15:00 起每小時失敗，Pushgateway 上
只剩 `last_run` 與 `log_errors=1`，`last_success` 整個不見，兩小時內沒有任何
告警，是人工查資料庫才發現的。

`pushadd_to_gateway()`（POST）只新增／更新這次推的指標，不動其他的。
代價是「某次推了之後再也不推的指標會留著舊值」，**這是刻意接受的取捨**：
留一個過期的值，遠好過整組告警靜默失效。

★ 這支換機器或退役時，**一定要刪掉舊的 grouping key**，否則會留下永遠不更新的
`last_success`，過了門檻時間後永久誤報且怎麼修都不會好：

```bash
curl -X DELETE "http://<pushgateway>/metrics/job/monitor_taipower_curve/instance_id/<舊的>/spider/loadcurve"
```

## ★ 排程要對齊時鐘，不可用 StartInterval

檔案在 **00:00 換日重置**，當日最後那幾個時間點過了午夜就**永久取不回來**
（那份來源沒有歷史）。而台電的發布延遲是 **7–11 分鐘**（2026-08-05 實測）。

`StartInterval` 的執行時刻由「載入當下」決定並會漂移：實測載入後變成每小時的
`:21` 跑，當天最後一次 23:21 只抓到 23:10，**23:20/23:30/23:40/23:50 四個點
每天固定遺失**（每天 64 列）。而且這種缺口在圖上看不出來——它長得就像
「那時候沒用電」。

所以用 `StartCalendarInterval` 對齊時鐘：每小時 `:55`，再加一次 `23:59` 收尾。
請求量幾乎不變（25 次/日 vs 24 次/日）。見 `deployment/*.plist`。

## 資料表

**不要自己建表、不要自己寫 migration。** 表由 dashboard-app 的 `alembic_monitor`
管理（已建好在正式庫），schema 見 `docs/SCHEMA.md`。這個專案只 INSERT/UPDATE。

★ 資料庫帳號用 **dashboard** 不是 crawler——`monitor_power_load_curve` 的
  寫入權限只授予 dashboard，用 crawler 連得上但 INSERT 會 permission denied。

★★ **每次執行都要另外寫一筆 `monitor_fetch_run`**（見 SCHEMA.md）。
   少了它，這支爬蟲在「資料健康」頁面上等於不存在，沒人知道它死了——
   而且因為前端有退回機制，畫面**看起來完全正常**。這條不能省。

## 時區

台電時戳是**台北時間且不帶時區**。組 `observed_at` 時必須明確補上 `+08:00`，
不可以當成 UTC。「今日」也要用台北時區的今天——用 UTC 日期會在早上 8 點前錯一天。

## 測試

至少要有：
1. 欄位對應測試（用 fixture，斷言燃氣/太陽能/風力等對到正確的值）
2. **兩支曲線總和吻合**的交叉測試 ← 最有價值的一條
3. 欄數變動要丟例外
4. 空白 → None 不是 0
5. 整點時間 `00` 與 `00:10` 兩種格式都能解析

寫完測試後**做負向對照**：故意把欄序改錯、把空白改成 0，確認測試會紅。
測試不會紅的話，那條測試等於沒寫。

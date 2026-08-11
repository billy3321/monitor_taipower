# CLAUDE.md — monitor_taipower_curve

這份是**規格與紀律**，不是建議。每一條都有踩過的實例，違反過的都寫在裡面。

## 這個專案要做的事

從一般網路的機器抓台電四支檔（三支 CSV ＋ `loadpara.json`），寫進 Cloud SQL 的
**`strait_info_monitor_prod`**，每小時一次。就這樣。
不要順手加視覺化、不要加 web 介面、不要加 ORM 以外的抽象層。

**DO NOT OVERDESIGN. DO NOT OVERENGINEER.**

★ 2026-08-06 起 `monitor_*` 表遷入爬蟲庫（先前在 `dashboard_monitor`）。
那個庫由 `monitor_strait_info` 擁有，本專案是**第二個寫入者，但只寫自己的兩張表**：
`monitor_power_load_curve` 與 `monitor_fetch_run`。**一表一寫入者**是平台原則——
不要跨線去寫別人的表，也不要因為「順手」就去讀改別人的資料。
dashboard-app 只負責讀取顯示。

★ 專案已經實作完成並在跑（2026-08-05 上線）。接手時要改東西，先跑
`./venv/bin/pytest -q`（88 條）與 `scripts/verify_fixtures.py` 確認基準線是綠的，
改完再跑一次。**負向對照**（見「測試」節）不是選配。

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

### ★ 四個陷阱

1. **同一份 HTML 裡有兩組設定，舊的那組是錯的。**
   `if (AmCharts.recommended()!='js' || msie)` 分支裡的 `<graphs><title>` 是
   **Flash 舊版**，它還列著已除役的核能，導致整組欄位位移——照它對會全錯。
   要看 `else` 之後那段（現行 JS 分支）的 balloon 文字。

2. **單位是萬瓩，不是 MW。** 1 萬瓩 = 10 MW。寫進資料庫前一律換算成 MW，
   跟 `monitor_power_unit_observation` 同單位。同一張圖上兩種單位遲早出事。

3. **時間欄有兩種寫法**：`00:10` 與整點的 `00`，同一個檔裡都會出現。

4. **★ 檔案永遠是 144 列，未來時段預先留白。**
   2026-08-05 12:20 的 fixture 實測：144 列裡只有 **75** 列有值
   （00:00–12:20 共 75 個 10 分鐘點），其餘 69 列的內容是 `,`
   （只有一個逗號，連時間欄都空）。這不是檔案壞掉，是當日尚未發生的時段。

   **絕對不可以把它們寫成 0 MW**——那會讓今天下午的用電看起來是零。
   正確做法是**整列跳過不寫入**：那些時間點根本還沒到，連「未報告」都算不上。

   ★ 但「欄位數不足就 skip」這句話要講精確，否則會跟「欄數不符要丟例外」
     互相矛盾。實作是三分（見 `parser.parse_curve`）：

     | 情況 | 處理 |
     |---|---|
     | 整列皆空（未來時段的 `,`）| 跳過，不寫入 |
     | 時間有值、欄數符合、個別數值空 | 該欄寫 `None`（未報告）|
     | 時間有值但**欄數不符** | **丟例外**，不要猜 |

   ★ 也因此，「這個檔有 144 列」不代表抓到完整一天。
     要判斷抓到多少，看的是**有值的列數**。

## 排錯先跑這兩支

```bash
./venv/bin/python scripts/preflight.py         # 這台機器抓不抓得到台電官網
./venv/bin/python scripts/verify_fixtures.py   # 欄位對應驗收，零相依
```

兩支都**不碰資料庫、不需要 config**，所以能乾淨地把「抓不到台電」跟
「寫不進資料庫」分開——這兩種失敗要修的東西完全不同。
`verify_fixtures.py` 附真實 fixture（`tests/fixtures/`，2026-08-05 實抓），
會直接印出正確的數字長什麼樣。

## 驗收：怎麼確認欄序沒對錯

**兩支曲線是同一份用電的兩種切分，同一時點的總和必須吻合**（實測差 1 MW 以內）。
標錯或漏掉任何一欄，兩邊總和就會分岔。**單看某一欄「看起來合理」驗不出欄序錯置**，
一定要用這個交叉檢查，並寫成測試。

參考值（2026-08-05 10:00）：能源別合計 38,103 MW、區域別合計 38,102 MW。

★★ **但總和檢查抓不到「欄位對調」**——把太陽能與重油互換，總和一模一樣。
2026-08-06 做負向對照時實測確認過這個盲點。所以三種檢查缺一不可：

| 檢查 | 抓得到什麼 |
|---|---|
| 兩曲線總和吻合 | 漏欄、多欄、單位錯 |
| 對特定欄斷言具體數值（燃氣/太陽能…）| **欄位對調** |
| 語意斷言（太陽能夜間必為 0、正午必高）| 欄位對調，且來源改版時也擋得住 |

第三條交叉檢查（2026-08-06 新增）：`loadpara.json` 的**即時用電**必須等於
同時點的能源別合計（實測差 0 MW）。它同時證明了「把 loadpara 掛在曲線最新時點」
這個做法是對的——那個檔沒有自己的時戳。

★ loadpara 偶爾比曲線**慢一個 10 分鐘檔**（2026-08-10 實測：早上爬升時段
一格差 462 MW）。`rehome_capacity` 會往回最多兩格找「即時用電＝合計」成立的
時點改掛——慢一格不算錯，掛回正確時點就好；連往回找都找不到才丟 capacity
並記錯誤。夜間平坦時多格都吻合，取最新的。

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
（launchd 的 StartCalendarInterval 跟著**系統時區**跑，這台機器必須是
Asia/Taipei——`readlink /etc/localtime` 驗證。）

### ★ 跨午夜防線：未來的點一律整批拒寫

23:59 那次若遇上慢網路（單一請求 timeout 25 秒），抓取可能跨過 00:00：
fuel 抓到舊日滿檔、之後的 genloadareaperc 已換日——日期基準取自後者，
舊日 23:50 會被標成**新一天的 23:50**，整天份的假資料寫進圖裡，
再被之後的執行一小時一小時慢慢蓋掉。

所以 `run_once` 在解析後檢查 `find_future_points()`：任何一點超過
now＋20 分鐘就**整批拒寫**（23:55 那次已把舊日收乾淨，丟掉這批零損失）。
容忍值 20 分鐘：要放得過正常發布延遲，也要抓得住錯標一天的資料。

### 已知的接受風險：23:50 那個點賭的是發布延遲 ≤ 9 分鐘

實測發布延遲 5–11 分鐘。23:59 收尾時 23:50 的點需要延遲 ≤9 分鐘才拿得到；
超過就只能放掉那一個點（不能再往後排——檔案 00:00 換日，之後抓到的
就是新天的檔，防線也會把它擋下）。實測 08-05、08-06 兩天都是完整 144 點。

## 資料表

**不要自己建表、不要自己寫 migration。** `monitor_*` 表由 `monitor_strait_info`
擁有與管理（已建好在正式庫），schema 見 `docs/SCHEMA.md`。
這個專案只對**自己那兩張表** INSERT/UPDATE。

★ 資料庫帳號用 **dashboard** 不是 crawler——這兩張表的寫入權限只授予 dashboard，
  用 crawler 連得上但 INSERT 會 permission denied，而且錯誤要跑到很後面才看得到。

★ 要加欄位得先跟表的擁有者講，由那邊開。不要因為「只是多一欄」就自己下 DDL。

★★ **每次執行都要另外寫一筆 `monitor_fetch_run`**（見 SCHEMA.md）。
   少了它，這支爬蟲在「資料健康」頁面上等於不存在，沒人知道它死了——
   而且因為前端有退回機制，畫面**看起來完全正常**。這條不能省。
   失敗也要寫（`status='error'`，`record_count` 是 **NULL 不是 0**）。

## 時區

台電時戳是**台北時間且不帶時區**。組 `observed_at` 時必須補上時區，
不可以當成 UTC。「今日」也要用台北時區的今天——用 UTC 日期會在早上 8 點前錯一天。

### ★★ 用 IANA 時區，不要硬編 `+08:00`

```python
from zoneinfo import ZoneInfo
TAIPEI = ZoneInfo('Asia/Taipei')            # ✓
TAIPEI = timezone(timedelta(hours=8))       # ✗ 不要
```

兩者對今天的資料算出來一樣，但意義不同：前者說的是「台北這個地方的時間」，
後者說的是「某個剛好是 +8 的偏移」。硬編偏移的東西一旦遇到時區規則變動
（台灣 1979 年以前實施過日光節約時間）就會靜靜地錯——而且錯的是**歷史資料重跑**，
那正是原文歸檔存在的目的。讓 tz 資料庫去回答偏移是多少，不要自己算。

★ 同理，`astimezone()` **不要不帶參數**：那會跟著這台機器的系統時區跑。
要台北就明寫 `astimezone(TAIPEI)`，要 UTC 就明寫。

★ 兩件事是獨立的，都要成立：**(a)** 每個寫進資料庫的 datetime 都是 aware 的；
**(b)** 時區是 IANA 時區。只做到 (a) 仍然會踩到硬編偏移的坑。
naive datetime 進到 `timestamptz` 欄位，資料庫會拿連線的 `TimeZone` 設定去猜，
猜錯就整批偏移，而且畫面看起來完全正常。

`tests/test_timezone.py` 把這兩條都釘住了，包括「原始碼裡不准再出現
`timedelta(hours=8)`」這種檢查。

## 測試

現有 88 條（`./venv/bin/pytest -q`）。動到解析或抓取就要跑，而且至少要保住這幾條：

1. 欄位對應（用 fixture，斷言燃氣/太陽能/風力等對到正確的值）
2. **兩支曲線總和吻合**的交叉測試 ← 最有價值的一條
3. 欄數變動要丟例外
4. 空白 → None 不是 0；`'0.0'` 要留成 0
5. 整點時間 `00` 與 `00:10` 兩種格式都能解析
6. 未來時段（`,`）整列跳過，不產生任何 mw=0 的點
7. `loadpara` 的即時用電＝同時點能源別合計
8. 時區：每個 datetime 都是 aware，且用 IANA 時區不是硬編 +08:00
9. 內容驗證：HTML 挑戰頁、空回應、JSON 端點吐 CSV 都要當失敗

### ★ 負向對照不是選配

寫完測試後**故意把它弄壞**，確認測試會紅。測試不會紅的話，那條測試等於沒寫。
2026-08-05／08-06 實際做過並確認有效的幾組：

| 改壞什麼 | 應該紅的測試 |
|---|---|
| `AREA_COLUMNS` 改成直覺的「東北中南」 | 區域欄位對應、`area_load` 對帳 |
| 空白 → `0.0` | 未知≠零 |
| 漏掉「汽電共生」一欄 | 兩曲線總和 |
| 太陽能與重油對調 | 具體數值斷言、太陽能夜間為 0 |
| 即時供電能力對到 `fore_*` | 兩個分母不同那條 |
| 拿掉 content-type 檢查 | HTML 挑戰頁 |
| `TAIPEI` 改回 `timezone(timedelta(hours=8))` | IANA 時區那兩條 |
| `astimezone(TAIPEI)` 改回裸 `astimezone()` | 歸檔不依賴機器時區那條 |
| 逐檔隔離改回一鍋端 | 壞 perc 不可拖垮曲線那條 |
| 未來時點容忍值改成無限大 | 跨午夜錯標那條 |
| rehome 不往回找（只看目前錨點）| loadpara 慢一格那條 |

★ 做負向對照時如果「改壞了測試卻還是綠的」，先確認不是 **Python bytecode 快取**
在騙你：改回去的檔案若**大小相同且在同一秒內寫入**，`.pyc` 的 (mtime, size)
驗證會判定快取仍有效而沿用舊 bytecode。踩過一次。先
`find . -name __pycache__ -exec rm -rf {} +` 再跑。

## 監控回報標準（五個爬蟲專案一致，2026-08-11 立）

**唯一事實來源**：`crawlers/monitoring_platform/docs/unified_api_and_communication_standard.md`
（Telemetry Standard v2）。動監控相關的東西前先看那份，不要各自發明。

這支專案必須做到：

1. **每次跑完都推 `scrapy_last_run_timestamp_seconds`，只有成功才推
   `scrapy_last_success_timestamp_seconds`**。存活告警看的是後者。
2. **一律用 `pushadd_to_gateway`（POST），不可用 `push_to_gateway`（PUT）**。
   PUT 是整組取代：失敗那次沒推 last_success，會把上次成功的紀錄一起洗掉，
   告警從此永遠不燒（2026-08-05 五專案同修的教訓）。
3. **`scrapy_items_unknown`：未知≠零**。抓失敗時筆數是「未知」，
   推 0 會被讀成「來源真的沒東西」——這兩件事的意義完全相反。
4. **`scrapy_max_stale_seconds`：把「自己多久沒成功算太舊」推上去**，
   讓告警用這個數字，不要在告警檔另外寫死一個。
   ★ 門檻寫兩份必定漂移：2026-08-11 台海情勢的告警寫死 26 小時，
     而 17 個來源的容許時間比它長，結果**週一跑完、週二就開始告警，
     一週 85% 的時間在喊狼來了**。
5. **抓取健康紀錄（成功或失敗）一定要寫進 DB，而且不能跟資料同一個交易**。
   ★ 2026-08-11 msil 事故：資料撞唯一鍵 → 整個交易 rollback →
     連「我失敗了」那筆紀錄也被丟掉 → 健康頁顯示「昨天成功、狀態正常」。
     **一個已經壞掉的來源，長得跟正常的一模一樣。**
     做法：rollback 後另開交易補寫，並照樣推 telemetry（ok=False）。
6. **一列寫不進去不要拖垮整批**。整批包一個 SAVEPOINT，整批失敗才退回逐列
   （壞列跳過、好列照寫），失敗列數要寫進健康紀錄並把該次狀態降為 error。
   救回來不等於沒事——靜默漏資料比整批失敗更危險。
7. **改 spider 名稱＝同一個 PR 改告警規則**。
   ★ 台海情勢改名後（facilities→pla_facilities 等），五條專屬告警規則
     一條都對不上，全變成永遠不會燒的規則——那看起來跟一切正常一模一樣。

參考實作：`monitor_strait_info/src/strait_info/fetchers/base.py`
（`_record_failed_write` 與 `write_rows`）、`fetchers/telemetry.py`。

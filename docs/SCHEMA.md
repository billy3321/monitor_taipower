# 資料表規格（與 dashboard-app 的契約）

**表由 dashboard-app 的 `alembic_monitor` 建立與管理，這個專案不做 migration。**
本檔是「這個爬蟲要寫成什麼形狀」的契約——欄位改動要兩邊一起。

資料庫：**`strait_info_monitor_prod`**（Cloud SQL，公開 IP + client certificate）

★ 2026-08-06 起從 `dashboard_monitor` 搬到這裡，與 `monitor_strait_info` 同庫。
舊庫的資料留著沒刪、最後一筆是 2026-08-06 15:55。**dashboard-app 那側的讀取
也要跟著指到新庫**，否則能源分頁會退回逐機組加總的粗版曲線——
而且畫面看起來完全正常，不會有人發現。

★ 搬家造成的一個帳面痕跡：**新庫今天的 `monitor_fetch_run` 少了 15:55 那一次**
（那次寫進舊庫，切換在 16:24）。所以資料健康頁上 14:55→16:24 會看到一個
約 1.5 小時的空檔。**那不是失敗**——曲線資料本身沒有缺口（當日累積檔在
16:24 那次把整天補齊了，實測今日 132 個時點、0 個斷點）。不需要回頭補那一列。

## `monitor_power_load_curve`

| 欄位 | 型別 | 說明 |
|---|---|---|
| `observed_at` | `timestamptz` **PK** | 曲線上該點的時間。台電時戳是**台北時間且不帶時區**，寫入前必須補 `+08:00` |
| `kind` | `text` **PK** | `fuel`＝依燃料類別／`area`＝依區域別 |
| `label` | `text` **PK** | 燃氣、燃煤…／東部、南部、中部、北部 |
| `mw` | `float` NULL | **NULL＝該點未報告，0＝真的零出力。** 來源單位是萬瓩，寫入前已 ×10 換算成 MW |
| `parser_version` | `text` | 解析規則版本，改欄位對應時要跟著改 |
| `fetched_at` | `timestamptz` | 這一筆是哪次執行寫的 |

索引：`(kind, observed_at)`——前端固定查「某個 kind 的今天」。

## 寫入方式

```sql
INSERT INTO monitor_power_load_curve (...)
VALUES (...)
ON CONFLICT (observed_at, kind, label) DO UPDATE
  SET mw = EXCLUDED.mw,
      parser_version = EXCLUDED.parser_version,
      fetched_at = EXCLUDED.fetched_at;
```

★ 是 `DO UPDATE` 不是 `DO NOTHING`：檔案是當日累積、每次抓都大量重疊，
而且**台電事後會修同一個時間點的值**。

## 第三支檔 `genloadareaperc.csv` 也存進同一張表

它只有一列、8 個數值，但**帶完整時戳**（另外兩支只有時分）：

```
2026-08-05 12:20,1304.1,1481.5,990.9,1076.7,1514.9,1230.1,29.2,50.8
```

欄序是 **北發電, 北用電, 中發電, 中用電, 南發電, 南用電, 東發電, 東用電**。
（驗證方式：官網顯示北發<用、中發<用、南發>用、東發<用，四組大小關係全對。）

**不需要新表**——拆成兩個 kind 塞進同一張：

| kind | label | 意義 |
|---|---|---|
| `area_gen` | 北部/中部/南部/東部 | 該區**發電** |
| `area_load` | 北部/中部/南部/東部 | 該區**用電** |

★ 這份資料的價值在於「發電與用電的差額就是區域間潮流」——北部長期發<用
（要靠中南部送電）、南部發>用。這是能源分頁上半部潮流圖的即時對照，
而潮流圖用的是**季度平均基線**（落後一季以上），兩者放一起才看得出今天異不異常。

### ★ 這份檔只回報「當下」，不是當日累積

另外兩支是當日累積檔（一抓拿回一整天 144 點），但這支**每次只有一列**，
所以 `area_gen`／`area_load` 的解析度就等於**執行頻率**（每小時一點），
而 `fuel`／`area` 是每 10 分鐘一點。中間漏掉的時間點**永久取不回來**。

**前端要照這個事實畫**：`area_gen` 是「各區現在發多少」的**即時數字**，
不要畫成連續曲線——畫成曲線會讓人以為中間那 50 分鐘是平的，那是假的。
★ 更不可以把沒抓到的時間點補 0 或內插。未知≠零。

★★ **`area_load` 與 `area` 是同一組數字**（2026-08-05 實測 12 組比對差 0.0 MW）。
區域用電已經在 `kind='area'` 裡有全天 10 分鐘解析度了，所以：

- 要**區域用電曲線** → 用 `area`（全天、10 分鐘一點）
- 要**區域發電** → 用 `area_gen`（每小時一點的即時值，這才是新資訊）
- `area_load` 只是留著方便跟 `area_gen` 同時點相減算潮流，不要拿它畫曲線

★ 一樣要換算：來源萬瓩 → MW（×10）。

## `loadpara.json` → `kind='capacity'`（2026-08-06 新增）

★★ **這是新的 kind，dashboard-app 那邊要知道。** 沒有新表也沒有新欄位，
沿用 `monitor_power_load_curve` 既有的形狀。

| label | 來源欄位 | 意義 |
|---|---|---|
| `即時用電` | `curr_load` | 當下用電（＝同時點能源別合計，實測差 0 MW）|
| `即時供電能力` | `real_hr_maxi_sply_capacity` | **當下**供電能力，每次抓都不同 |
| `今日最大供電能力` | `fore_maxi_sply_capacity` | 今日**預估**值，一天固定 |
| `尖峰預估用電` | `fore_peak_dema_load` | 今日預估尖峰負載 |
| `尖峰預估備轉容量` | `fore_peak_resv_capacity` | 今日預估尖峰備轉 |

★★ **儀表刻度的分母要用「即時供電能力」**，不是「今日最大供電能力」——
台電網頁上的「使用率 %」用的是前者。2026-08-06 實測：
即時用電 40,582 MW ÷ 即時供電能力 49,879 MW = 81%（與網頁一致），
÷ 今日最大供電能力 49,551 MW = 82%（差一個百分點）。

★ **這個檔沒有自己的時戳**（`publish_time` 是預估值的發布時間，不是 `curr_load`
的時間）。`observed_at` 掛在**同次抓到的曲線最新時點**上。這不是將就：
`curr_load` 與該時點的能源別合計實測完全相同，而且每次執行都會重驗
（差 ≥100 MW 就不寫 capacity 並記一次錯誤）。

★ 一樣是萬瓩 → MW（×10）。

**沒有進資料庫的欄位**：使用率 `curr_util_rate`、備轉容量率
`fore_peak_resv_rate`、燈號 `fore_peak_resv_indicator`、尖峰時段
`fore_peak_hour_range`、發布時間 `publish_time`、昨日摘要 `yday_*`。
理由是 `mw` 欄位的語意就是 MW，把百分比塞進去遲早有人拿它去加總；
文字欄更沒地方放。**這些全部保存在原文歸檔裡**（見下），
而且比率本來就能從 MW 值回推。若 dashboard 需要把它們入庫，
請那邊開欄位，這裡再補寫。

## 原文歸檔與 `raw_uri` / `raw_sha256` 的語意

★ 平台紀律「原文全存」的落實方式（2026-08-06 起）：

- `raw_uri` = 這次執行的**歸檔目錄**絕對路徑（在那台 Mac 上），
  例如 `/Users/.../data/raw/2026-08-06/142044`
- `raw_sha256` = 該目錄下 `MANIFEST.txt` 的 sha256
- `MANIFEST.txt` 每行是「檔名 → 來源網址 → bytes → 該檔 sha256」，
  所以驗 MANIFEST 一份等於驗全部四支檔

★ 這改變了 `raw_uri` 先前的語意（原本放 base URL）。來源網址現在記在
MANIFEST 裡，每支檔各自對應——比一個共用的 base URL 精確。

## label 的合法值

`kind='fuel'`（12 個，順序即堆疊順序）：

```
燃氣, 民營電廠-燃氣, 燃煤, 民營電廠-燃煤, 汽電共生, 重油,
太陽能, 風力, 水力, 儲能, 其它再生能源, 儲能負載
```

`kind='area'`（4 個，順序即堆疊順序）：

```
東部, 南部, 中部, 北部
```

`kind='area_gen'` / `kind='area_load'`（各 4 個）：

```
北部, 中部, 南部, 東部
```

`kind='capacity'`（5 個，2026-08-06 新增）：

```
即時用電, 即時供電能力, 今日最大供電能力, 尖峰預估用電, 尖峰預估備轉容量
```

★ 這兩組順序**就是台電圖上由下往上的堆疊順序**，dashboard-app 會照這個順序畫。
不要自己重排；來源改版時兩邊一起改。

## ★★ 每次執行都要再寫一筆 `monitor_fetch_run`（不可省略）

只寫曲線資料的話，這支爬蟲在「資料健康」頁面上**不存在**——沒人知道它活著還是死了。
dashboard 的健康度、新鮮度、缺口偵測全部 key 在 `monitor_fetch_run`，
所以中繼必須跟其他 fetcher 一樣，每跑一次就 append 一列：

```sql
INSERT INTO monitor_fetch_run
  (source_id, fetched_at, status, record_count, data_timestamp, covered_span, http_status, note)
VALUES ('taipower_loadcurve', now(), 'ok', 1234, <最新那點的時間>,
        tstzrange(<最早那點>, <最新那點>, '[]'), 200, 'fuel=900; area=300; areaperc=8');
```

欄位語意（**這幾條寫錯會讓監控說謊**）：

| 欄位 | 規則 |
|---|---|
| `source_id` | 固定 `taipower_loadcurve`（已登記在 registry） |
| `status` | `ok` / `error` / `no_coverage`。**失敗也要寫一列**，不是不寫 |
| `record_count` | **失敗時是 NULL 不是 0**。0 的意思是「確實沒有紀錄」，跟「沒抓到」是兩件事 |
| `data_timestamp` | 曲線上最新那個點的時間，不是執行時間 |
| `covered_span` | 這次抓到的資料涵蓋的區間（最早點→最新點）。缺口偵測靠它 |

★ 這是 append-only 表：一次執行一列，不覆蓋不更新。

★ `monitor_source` 裡已經有 `taipower_loadcurve` 這筆（dashboard-app 的 registry
  已登記並 sync），外鍵不會擋。若插入時報外鍵錯誤，表示 registry 還沒 sync，
  在 dashboard-app 那邊跑 `scripts/monitor/sync_registry.py`。

## dashboard-app 那側怎麼用

- 端點：`GET /api/v1/monitor/energy/load-curve?kind=fuel|area`
- 服務：`EnergySlotService.load_curve()`
- ★ 這張表**沒有今日資料時會自動退回**「從 `monitor_power_unit_observation`
  逐機組加總」的粗版曲線（約每小時一點），畫面上會標明當下顯示的是哪一種。
  所以這台 Mac 掛掉不會讓能源分頁開天窗——但也因此**不會自己被發現**，
  存活告警一定要設。

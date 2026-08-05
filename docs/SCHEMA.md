# 資料表規格（與 dashboard-app 的契約）

**表由 dashboard-app 的 `alembic_monitor` 建立與管理，這個專案不做 migration。**
本檔是「這個爬蟲要寫成什麼形狀」的契約——欄位改動要兩邊一起。

資料庫：`dashboard_monitor`（Cloud SQL，公開 IP + client certificate）

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

★ 一樣要換算：來源萬瓩 → MW（×10）。

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

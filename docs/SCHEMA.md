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

★ 這兩組順序**就是台電圖上由下往上的堆疊順序**，dashboard-app 會照這個順序畫。
不要自己重排；來源改版時兩邊一起改。

## dashboard-app 那側怎麼用

- 端點：`GET /api/v1/monitor/energy/load-curve?kind=fuel|area`
- 服務：`EnergySlotService.load_curve()`
- ★ 這張表**沒有今日資料時會自動退回**「從 `monitor_power_unit_observation`
  逐機組加總」的粗版曲線（約每小時一點），畫面上會標明當下顯示的是哪一種。
  所以這台 Mac 掛掉不會讓能源分頁開天窗——但也因此**不會自己被發現**，
  存活告警一定要設。

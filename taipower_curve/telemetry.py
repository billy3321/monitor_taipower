"""推遙測到 Pushgateway（家族契約 Telemetry Standard v2）。

全部是 gauge、單一 scrapy_ 前綴、無 _total。推送失敗只 WARNING 不中斷爬蟲。

★ 存活告警是 `time() - scrapy_last_success_timestamp_seconds > 門檻`，
  不可用 `up`——Pushgateway 的 up 永遠是 1。
"""
import logging

from prometheus_client import CollectorRegistry, Gauge, pushadd_to_gateway

log = logging.getLogger(__name__)

JOB = 'monitor_taipower_curve'
SPIDER = 'loadcurve'
PUSH_TIMEOUT = 5.0


def push(cfg: dict, *, run_ts: float, success: bool, items: int,
         errors: int, duration: float) -> None:
    """推一次遙測。★ 失敗的執行也要推——沒有遙測 = 爬蟲死了沒人知道。"""
    mon = (cfg.get('monitoring') or {})
    pg = (mon.get('pushgateway') or {})
    if not pg.get('enabled'):
        log.warning('遙測未啟用（monitoring.pushgateway.enabled=false）——'
                    '這支爬蟲的存活狀態目前沒有人在看')
        return
    url = pg.get('url', '')
    if not url or 'CHANGE_ME' in url:
        log.warning('遙測 URL 沒設好（%s），跳過推送', url or '空')
        return

    registry = CollectorRegistry()
    Gauge('scrapy_last_run_timestamp_seconds', '最後一次執行的時間',
          registry=registry).set(run_ts)
    Gauge('scrapy_items_scraped', '這次寫入的資料點數',
          registry=registry).set(items)
    Gauge('scrapy_log_errors', '這次執行的錯誤數',
          registry=registry).set(errors)
    Gauge('scrapy_run_duration_seconds', '這次執行耗時',
          registry=registry).set(duration)
    if success:
        # ★ 僅成功時設。失敗時**不推這個指標**，讓它保留上次成功的時間，
        #   存活告警才算得出「多久沒成功了」。
        Gauge('scrapy_last_success_timestamp_seconds', '最後一次成功的時間',
              registry=registry).set(run_ts)

    try:
        # ★★ 一定要用 pushadd（POST）不是 push（PUT）。
        #    PUT 會**替換掉**同一個 grouping key 底下的所有指標——失敗的執行
        #    沒推 last_success，PUT 就會把上一次成功的時戳一起刪掉，
        #    告警式子從「值很舊」變成「查無資料」，反而不會響。
        pushadd_to_gateway(
            url, job=JOB, registry=registry,
            grouping_key={'instance_id': mon.get('instance_id', 'unknown'),
                          'spider': SPIDER},
            timeout=PUSH_TIMEOUT)
        log.info('遙測已推送到 %s（items=%d errors=%d success=%s）',
                 url, items, errors, success)
    except Exception as exc:                      # noqa: BLE001 — 推送失敗不該中斷爬蟲
        log.warning('遙測推送失敗（不影響爬取）：%s — %s', type(exc).__name__, exc)

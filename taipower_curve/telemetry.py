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

# ★ 標準 #4：把「自己多久沒成功算太舊」隨每次執行推上去，讓告警用這個數字。
#   門檻寫兩份（這裡一份、告警檔一份）必定漂移——台海情勢就漂移過，
#   一週 85% 的時間在誤報。每小時跑一次，連續 3 次失敗（3 小時）算死亡。
MAX_STALE_SECONDS = 3 * 3600


def push(cfg: dict, *, run_ts: float, status: str, items: int,
         errors: int, duration: float) -> None:
    """推一次遙測。★ 失敗的執行也要推——沒有遙測 = 爬蟲死了沒人知道。

    status: 'ok' / 'no_coverage' / 'error'（與 monitor_fetch_run 同語意）。
    """
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

    success = status == 'ok'
    # ★ 標準 #3：未知≠零。error 且一筆都沒寫成時，筆數是「未知」——
    #   推 items_scraped=0 會被讀成「來源真的沒東西」，意義完全相反。
    #   此時不推 items_scraped（留上次的值），改推 items_unknown=1。
    #   ok / no_coverage（確實沒有紀錄）/ 有寫入的部分失敗，筆數都是可信的。
    items_known = status in ('ok', 'no_coverage') or items > 0

    registry = CollectorRegistry()
    Gauge('scrapy_last_run_timestamp_seconds', '最後一次執行的時間',
          registry=registry).set(run_ts)
    Gauge('scrapy_log_errors', '這次執行的錯誤數',
          registry=registry).set(errors)
    Gauge('scrapy_run_duration_seconds', '這次執行耗時',
          registry=registry).set(duration)
    Gauge('scrapy_max_stale_seconds', '多久沒成功算太舊（告警門檻）',
          registry=registry).set(MAX_STALE_SECONDS)
    Gauge('scrapy_items_unknown', '這次的筆數是否為未知（1=未知，勿當 0 讀）',
          registry=registry).set(0 if items_known else 1)
    if items_known:
        Gauge('scrapy_items_scraped', '這次寫入的資料點數',
              registry=registry).set(items)
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
        log.info('遙測已推送到 %s（status=%s items=%s errors=%d）',
                 url, status, items if items_known else '未知', errors)
    except Exception as exc:                      # noqa: BLE001 — 推送失敗不該中斷爬蟲
        log.warning('遙測推送失敗（不影響爬取）：%s — %s', type(exc).__name__, exc)

"""遙測契約的測試（Telemetry Standard v2，2026-08-11 立的五專案標準）。

不打網路：把 pushadd_to_gateway 換成捕捉器，檢查 registry 裡實際會推什麼。
"""
import pytest

from taipower_curve import telemetry


CFG = {'monitoring': {'pushgateway': {'enabled': True, 'url': 'http://x:1'},
                      'instance_id': 'unit-test'}}


@pytest.fixture
def pushed(monkeypatch):
    calls = []

    def fake(url, job, registry, grouping_key, timeout):
        calls.append({'url': url, 'job': job, 'registry': registry,
                      'grouping_key': grouping_key, 'timeout': timeout})

    monkeypatch.setattr(telemetry, 'pushadd_to_gateway', fake)
    return calls


def metrics(call) -> dict[str, float]:
    return {s.name: s.value
            for m in call['registry'].collect() for s in m.samples}


def test_ok_run_pushes_full_set(pushed):
    telemetry.push(CFG, run_ts=1000.0, status='ok', items=1234,
                   errors=0, duration=5.0)
    m = metrics(pushed[0])
    assert m['scrapy_last_run_timestamp_seconds'] == 1000.0
    assert m['scrapy_last_success_timestamp_seconds'] == 1000.0
    assert m['scrapy_items_scraped'] == 1234
    assert m['scrapy_items_unknown'] == 0
    assert m['scrapy_log_errors'] == 0
    assert m['scrapy_max_stale_seconds'] == telemetry.MAX_STALE_SECONDS
    assert m['scrapy_run_duration_seconds'] == 5.0


def test_failed_run_reports_unknown_not_zero(pushed):
    """★ 標準 #3：未知≠零。抓失敗時筆數是「未知」——
    推 items_scraped=0 會被讀成「來源真的沒東西」，意義完全相反。"""
    telemetry.push(CFG, run_ts=2000.0, status='error', items=0,
                   errors=2, duration=1.0)
    m = metrics(pushed[0])
    assert 'scrapy_items_scraped' not in m, '失敗時不可推 0'
    assert m['scrapy_items_unknown'] == 1
    assert 'scrapy_last_success_timestamp_seconds' not in m, '僅成功時設'
    assert m['scrapy_last_run_timestamp_seconds'] == 2000.0


def test_partial_write_error_has_known_count(pushed):
    """部分寫入的 error（例如 loadpara 壞但曲線有寫）：筆數是可信的。"""
    telemetry.push(CFG, run_ts=3000.0, status='error', items=936,
                   errors=1, duration=1.0)
    m = metrics(pushed[0])
    assert m['scrapy_items_scraped'] == 936
    assert m['scrapy_items_unknown'] == 0
    assert 'scrapy_last_success_timestamp_seconds' not in m


def test_no_coverage_zero_is_a_real_zero(pushed):
    """no_coverage＝「確實沒有紀錄」——0 是真的 0，不是未知。"""
    telemetry.push(CFG, run_ts=4000.0, status='no_coverage', items=0,
                   errors=0, duration=1.0)
    m = metrics(pushed[0])
    assert m['scrapy_items_scraped'] == 0
    assert m['scrapy_items_unknown'] == 0
    assert 'scrapy_last_success_timestamp_seconds' not in m


def test_max_stale_pushed_every_run(pushed):
    """★ 標準 #4：門檻寫兩份必定漂移，告警要用推上去的這個數字。"""
    for status in ('ok', 'error', 'no_coverage'):
        telemetry.push(CFG, run_ts=1.0, status=status, items=0,
                       errors=0, duration=0.1)
    assert all(metrics(c)['scrapy_max_stale_seconds'] == 3 * 3600
               for c in pushed)


def test_grouping_key_and_job_match_contract(pushed):
    telemetry.push(CFG, run_ts=1.0, status='ok', items=1, errors=0, duration=0.1)
    call = pushed[0]
    assert call['job'] == 'monitor_taipower_curve'
    assert call['grouping_key'] == {'instance_id': 'unit-test',
                                    'spider': 'loadcurve'}
    assert call['timeout'] == 5.0


def test_disabled_pushes_nothing(pushed):
    telemetry.push({'monitoring': {'pushgateway': {'enabled': False}}},
                   run_ts=1.0, status='ok', items=1, errors=0, duration=0.1)
    assert pushed == []

"""loadpara.json（即時供電能力）的解析測試。

fixture 是 2026-08-06 14:1x 實抓。
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from taipower_curve import parser as P

FIXTURES = Path(__file__).parent / 'fixtures'
TAIPEI = timezone(timedelta(hours=8))
WHEN = datetime(2026, 8, 6, 14, 10, tzinfo=TAIPEI)


def read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def cap():
    return P.parse_loadpara(read('loadpara.json'), WHEN)


def test_fields_map_to_expected_values(cap):
    """萬瓩 ×10 → MW。★ 換算錯會差 10 倍。"""
    got = {p.label: p.mw for p in cap}
    assert got['即時用電'] == 40582.0            # curr_load 4058.2
    assert got['即時供電能力'] == 49879.0        # real_hr_maxi_sply_capacity 4987.9
    assert got['今日最大供電能力'] == 49551.0    # fore_maxi_sply_capacity 4955.1
    assert got['尖峰預估用電'] == 41000.0        # fore_peak_dema_load 4100.0
    assert got['尖峰預估備轉容量'] == 8551.0     # fore_peak_resv_capacity 855.1


def test_instant_capacity_differs_from_daily_forecast(cap):
    """★★ 這就是這支檔要抓的理由：兩個分母不一樣。

    台電網頁的「使用率」用**即時供電能力**，而不是今日最大供電能力。
    拿錯分母算出來的百分比會差約 1 個百分點。
    """
    got = {p.label: p.mw for p in cap}
    instant, forecast = got['即時供電能力'], got['今日最大供電能力']
    assert instant != forecast
    load = got['即時用電']
    assert round(load / instant * 100) == 81      # 網頁顯示的 curr_util_rate
    assert round(load / forecast * 100) == 82     # 用錯分母就變 82


def test_all_points_are_capacity_kind_at_given_time(cap):
    assert {p.kind for p in cap} == {'capacity'}
    assert {p.observed_at for p in cap} == {WHEN}
    assert len(cap) == len(P.LOADPARA_FIELDS)


def test_percentage_and_text_fields_are_not_stored(cap):
    """★ 百分比與文字欄刻意不進資料庫——mw 欄位的語意就是 MW。

    它們全部保存在原文歸檔裡，而且比率能從 MW 值回推。
    """
    labels = {p.label for p in cap}
    for forbidden in ('使用率', '備轉容量率', 'publish_time', '尖峰時段'):
        assert forbidden not in labels


def test_yesterday_summary_not_stored(cap):
    """yday_* 是昨天的摘要，我們自己的曲線已經有昨天全天資料。"""
    assert not any('昨' in p.label for p in cap)


# ── 壞資料要丟例外，不要猜 ──────────────────────────────────────

def test_malformed_json_raises():
    with pytest.raises(P.ParseError, match='不是合法 JSON'):
        P.parse_loadpara(b'{"records": [', WHEN)


def test_missing_records_key_raises():
    with pytest.raises(P.ParseError, match='結構不符預期'):
        P.parse_loadpara(b'{"success": "true"}', WHEN)


def test_missing_expected_field_raises():
    """欄位消失＝來源改版，要看得見。"""
    body = b'{"records": [{"curr_load": "4058.2"}]}'
    with pytest.raises(P.ParseError, match='缺少預期欄位'):
        P.parse_loadpara(body, WHEN)


def test_html_challenge_page_raises():
    with pytest.raises(P.ParseError, match='不是合法 JSON'):
        P.parse_loadpara(b'<!DOCTYPE html><html><body>blocked</body></html>', WHEN)


# ── 未知≠零 ──────────────────────────────────────────────────

def test_blank_capacity_is_none_not_zero():
    body = ('{"records": [{"curr_load": "", "real_hr_maxi_sply_capacity": "4987.9",'
            '"fore_maxi_sply_capacity": "4955.1", "fore_peak_dema_load": "4100.0",'
            '"fore_peak_resv_capacity": "855.1"}]}').encode()
    got = {p.label: p.mw for p in P.parse_loadpara(body, WHEN)}
    assert got['即時用電'] is None, '空值必須是 None 不是 0'
    assert got['即時供電能力'] == 49879.0


# ── 與曲線的交叉檢查 ──────────────────────────────────────────

def test_curr_load_matches_fuel_total_via_parse_all():
    """★ 第三條交叉驗收：loadpara 的即時用電＝同時點能源別合計。

    這同時證明「把 loadpara 掛在曲線最新時點」這個做法是對的。
    """
    pts = P.parse_all(read('loadfueltype.csv'), read('loadareas.csv'),
                      read('genloadareaperc.csv'), read('loadpara.json'))
    checked = P.cross_check_capacity(pts)
    assert checked is not None
    curr, fuel_total = checked
    # fixture 的曲線是 08-05、loadpara 是 08-06，數值本來就不同；
    # 這裡只驗機制接得起來，數值一致性由 test_live 那條在真實資料上驗。
    assert curr > 0 and fuel_total > 0


def test_loadpara_hangs_on_latest_curve_timepoint():
    pts = P.parse_all(read('loadfueltype.csv'), read('loadareas.csv'),
                      read('genloadareaperc.csv'), read('loadpara.json'))
    cap_times = {p.observed_at for p in pts if p.kind == 'capacity'}
    fuel_times = {p.observed_at for p in pts if p.kind == 'fuel'}
    assert cap_times == {max(fuel_times)}


def test_no_curve_means_no_capacity_points():
    """★ 沒有曲線就沒有可信的時戳可掛——寧可不寫，也不要自己編一個時間。"""
    pts = P.parse_all(None, None, None, read('loadpara.json'))
    assert [p for p in pts if p.kind == 'capacity'] == []


# ── rehome：loadpara 慢曲線一格時改掛正確時點 ────────────────────

def _fuel(t, total):
    return P.Point(t, 'fuel', '燃氣', total)


def _capacity(t, curr):
    return [P.Point(t, 'capacity', '即時用電', curr),
            P.Point(t, 'capacity', '即時供電能力', 49879.0)]


def _t(hh, mm):
    return datetime(2026, 8, 10, hh, mm, tzinfo=P.TAIPEI)


def test_rehome_keeps_anchor_when_in_sync():
    pts = [_fuel(_t(9, 20), 36084.0), _fuel(_t(9, 30), 36545.0),
           *_capacity(_t(9, 30), 36545.0)]
    out, anchored, diff = P.rehome_capacity(pts)
    assert anchored == _t(9, 30)
    assert diff == 0.0
    assert out == pts, '同步時不該動任何東西'


def test_rehome_moves_capacity_one_slot_back():
    """★ 重現 2026-08-10 09:32 事故：loadpara 還是 09:20 的值（36083），
    曲線已出 09:30（36545）。改掛 09:20 而不是丟掉。"""
    pts = [_fuel(_t(9, 20), 36084.0), _fuel(_t(9, 30), 36545.0),
           *_capacity(_t(9, 30), 36083.0)]
    out, anchored, diff = P.rehome_capacity(pts)
    assert anchored == _t(9, 20)
    assert diff == 1.0
    cap_times = {p.observed_at for p in out if p.kind == 'capacity'}
    assert cap_times == {_t(9, 20)}, 'capacity 全部點都要跟著改掛'
    fuel_times = {p.observed_at for p in out if p.kind == 'fuel'}
    assert fuel_times == {_t(9, 20), _t(9, 30)}, '曲線不可以被動到'


def test_rehome_prefers_latest_when_flat():
    """夜間負載平坦時多個時點都吻合——取最新的（loadpara 是當下的值）。"""
    pts = [_fuel(_t(2, 0), 25000.0), _fuel(_t(2, 10), 25010.0),
           *_capacity(_t(2, 10), 25005.0)]
    _, anchored, _ = P.rehome_capacity(pts)
    assert anchored == _t(2, 10)


def test_rehome_gives_up_beyond_max_back():
    """慢超過 max_back 格＝真的不同步，回 None 讓呼叫端丟掉並記錯誤。"""
    pts = [_fuel(_t(9, 0), 35274.0), _fuel(_t(9, 10), 36092.0),
           _fuel(_t(9, 20), 36084.0), _fuel(_t(9, 30), 36545.0),
           *_capacity(_t(9, 30), 35274.0)]           # 只跟三格前的 09:00 吻合
    assert P.rehome_capacity(pts, max_back=2) is None


def test_rehome_gives_up_when_nothing_matches():
    pts = [_fuel(_t(9, 20), 36084.0), _fuel(_t(9, 30), 36545.0),
           *_capacity(_t(9, 30), 99999.0)]
    assert P.rehome_capacity(pts) is None


def test_rehome_refuses_when_curr_load_unreported():
    """即時用電是 None 時驗不了時點——寧可不寫，不要掛在猜的時間上。"""
    pts = [_fuel(_t(9, 30), 36545.0),
           P.Point(_t(9, 30), 'capacity', '即時用電', None),
           P.Point(_t(9, 30), 'capacity', '即時供電能力', 49879.0)]
    assert P.rehome_capacity(pts) is None

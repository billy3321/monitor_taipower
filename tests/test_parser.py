"""欄位對應與解析紀律的測試。

用 tests/fixtures/（2026-08-05 12:20 實抓）。這些測試就是驗收條件——
改欄位對應而這裡沒紅，表示測試沒寫對。
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from taipower_curve import parser as P

FIXTURES = __import__('pathlib').Path(__file__).parent / 'fixtures'
BASE_DATE = date(2026, 8, 5)
TAIPEI = timezone(timedelta(hours=8))


def read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def fuel():
    return P.parse_curve(read('loadfueltype.csv'), 'fuel', P.FUEL_COLUMNS, BASE_DATE)


@pytest.fixture
def area():
    return P.parse_curve(read('loadareas.csv'), 'area', P.AREA_COLUMNS, BASE_DATE)


def value_at(points, label, when):
    return next(p.mw for p in points if p.label == label and p.observed_at == when)


# ── 1. 欄位對應 ────────────────────────────────────────────────────

def test_fuel_columns_map_to_expected_values(fuel):
    """12:20 那列：1508.8,493.4,698.0,111.1,202.4,33.8,881.1,44.3,41.9,0.0,4.0,-179.8

    ★ 斷言的是「第 n 欄對到哪個名字」。欄序錯置時這裡會紅。
    """
    t = datetime(2026, 8, 5, 12, 20, tzinfo=TAIPEI)
    assert value_at(fuel, '燃氣', t) == 15088.0
    assert value_at(fuel, '民營電廠-燃氣', t) == 4934.0
    assert value_at(fuel, '燃煤', t) == 6980.0
    assert value_at(fuel, '民營電廠-燃煤', t) == 1111.0
    assert value_at(fuel, '汽電共生', t) == 2024.0
    assert value_at(fuel, '重油', t) == 338.0
    assert value_at(fuel, '太陽能', t) == 8811.0
    assert value_at(fuel, '風力', t) == 443.0
    assert value_at(fuel, '水力', t) == 419.0
    assert value_at(fuel, '儲能', t) == 0.0
    assert value_at(fuel, '其它再生能源', t) == 40.0
    assert value_at(fuel, '儲能負載', t) == -1798.0


def test_area_columns_are_east_south_central_north(area):
    """★ 不是「東北中南」。最後一欄是北部——用官網當日數字對帳確認過。"""
    t = datetime(2026, 8, 5, 12, 20, tzinfo=TAIPEI)
    assert value_at(area, '東部', t) == 508.0
    assert value_at(area, '南部', t) == 12301.0
    assert value_at(area, '中部', t) == 10767.0
    assert value_at(area, '北部', t) == 14815.0


def test_solar_is_zero_at_night_and_high_at_noon(fuel):
    """太陽能對錯欄的話這裡也會紅：夜間必為 0、正午必然很高。"""
    midnight = datetime(2026, 8, 5, 0, 0, tzinfo=TAIPEI)
    noon = datetime(2026, 8, 5, 12, 0, tzinfo=TAIPEI)
    assert value_at(fuel, '太陽能', midnight) == 0.0
    assert value_at(fuel, '太陽能', noon) > 5000.0


# ── 2. 兩支曲線總和吻合（最有價值的一條）────────────────────────────

def test_two_curves_agree_at_every_common_timestamp(fuel, area):
    """★ 能源別與區域別是同一份用電的兩種切分，每個共同時點總和都該吻合。

    標錯或漏掉任何一欄就會分岔。單看某一欄「看起來合理」驗不出欄序錯置。
    """
    points = fuel + area
    common = ({p.observed_at for p in fuel} & {p.observed_at for p in area})
    assert len(common) == 75, 'fixture 應有 75 個有值時點'
    for t in sorted(common):
        f = P.totals_at(points, 'fuel', t)
        a = P.totals_at(points, 'area', t)
        assert abs(f - a) < P.CROSS_CHECK_TOLERANCE_MW, f'{t} 兩邊總和分岔：{f} vs {a}'


def test_reference_totals_at_1000(fuel, area):
    """CLAUDE.md 記的參考值：2026-08-05 10:00 能源別 38,103 MW、區域別 38,102 MW。"""
    t = datetime(2026, 8, 5, 10, 0, tzinfo=TAIPEI)
    assert round(P.totals_at(fuel, 'fuel', t)) == 38103
    assert round(P.totals_at(area, 'area', t)) == 38102


def test_cross_check_helper_picks_latest_common_point(fuel, area):
    t, f, a = P.cross_check(fuel + area)
    assert t == datetime(2026, 8, 5, 12, 20, tzinfo=TAIPEI)
    assert abs(f - a) < P.CROSS_CHECK_TOLERANCE_MW


def test_storage_load_is_negative_and_counted_in_total(fuel):
    """★ 儲能負載是負的，那是正常的（充電側）。不要濾掉、不要取絕對值。"""
    t = datetime(2026, 8, 5, 12, 20, tzinfo=TAIPEI)
    assert value_at(fuel, '儲能負載', t) == -1798.0
    total = P.totals_at(fuel, 'fuel', t)
    naive = sum(p.mw for p in fuel
                if p.observed_at == t and p.mw is not None and p.label != '儲能負載')
    assert total == pytest.approx(naive - 1798.0), '儲能負載必須計入合計'


# ── 3. 欄數變動要丟例外 ────────────────────────────────────────────

def test_extra_column_raises():
    body = b'00:10,1.0,2.0,3.0,4.0,5.0\n'
    with pytest.raises(P.ParseError, match='欄數'):
        P.parse_curve(body, 'area', P.AREA_COLUMNS, BASE_DATE)


def test_missing_column_raises():
    body = b'00:10,1.0,2.0,3.0\n'
    with pytest.raises(P.ParseError, match='欄數'):
        P.parse_curve(body, 'area', P.AREA_COLUMNS, BASE_DATE)


def test_values_without_time_raises():
    body = b',1.0,2.0,3.0,4.0\n'
    with pytest.raises(P.ParseError, match='沒有時間'):
        P.parse_curve(body, 'area', P.AREA_COLUMNS, BASE_DATE)


def test_unrecognised_token_raises():
    """認不得的字面要丟例外，不要默默當 None——那是來源改版的訊號。"""
    body = b'00:10,1.0,abc,3.0,4.0\n'
    with pytest.raises(P.ParseError, match='認不得'):
        P.parse_curve(body, 'area', P.AREA_COLUMNS, BASE_DATE)


# ── 4. 未知≠零 ────────────────────────────────────────────────────

def test_blank_becomes_none_not_zero():
    body = b'00:10,,1.0,-,4.0\n'
    pts = P.parse_curve(body, 'area', P.AREA_COLUMNS, BASE_DATE)
    got = {p.label: p.mw for p in pts}
    assert got['東部'] is None, '空字串必須是 None 不是 0'
    assert got['中部'] is None, "'-' 必須是 None 不是 0"
    assert got['南部'] == 10.0
    assert got['北部'] == 40.0


def test_explicit_zero_stays_zero():
    """'0.0' 才是真的零，不可以變成 None。"""
    body = b'00:10,0.0,1.0,2.0,3.0\n'
    pts = P.parse_curve(body, 'area', P.AREA_COLUMNS, BASE_DATE)
    east = next(p for p in pts if p.label == '東部')
    assert east.mw == 0.0
    assert east.mw is not None


def test_none_is_excluded_from_totals_not_treated_as_zero():
    body = b'00:10,,1.0,2.0,3.0\n'
    pts = P.parse_curve(body, 'area', P.AREA_COLUMNS, BASE_DATE)
    t = datetime(2026, 8, 5, 0, 10, tzinfo=TAIPEI)
    assert P.totals_at(pts, 'area', t) == 60.0


# ── 5. 兩種時間格式 ───────────────────────────────────────────────

def test_bare_hour_and_hhmm_both_parse():
    """同一個檔裡兩種寫法都會出現：整點的 '00' 與 '00:10'。"""
    assert P.parse_time('00', BASE_DATE) == datetime(2026, 8, 5, 0, 0, tzinfo=TAIPEI)
    assert P.parse_time('00:10', BASE_DATE) == datetime(2026, 8, 5, 0, 10, tzinfo=TAIPEI)
    assert P.parse_time('23:50', BASE_DATE) == datetime(2026, 8, 5, 23, 50, tzinfo=TAIPEI)


def test_areas_fixture_first_row_uses_bare_hour(area):
    """loadareas.csv 首列實測就是 '00'（不是 '00:00'）。"""
    assert min(p.observed_at for p in area) == datetime(2026, 8, 5, 0, 0, tzinfo=TAIPEI)


def test_observed_at_carries_taipei_offset(fuel):
    """★ 台電時戳不帶時區，必須明確補 +08:00，不可當成 UTC。"""
    for p in fuel[:20]:
        assert p.observed_at.utcoffset() == timedelta(hours=8)


def test_bad_time_raises():
    with pytest.raises(P.ParseError):
        P.parse_time('25:00', BASE_DATE)
    with pytest.raises(P.ParseError):
        P.parse_time('晚上八點', BASE_DATE)


# ── 6. 未來時段整列跳過 ───────────────────────────────────────────

def test_future_slots_are_skipped_entirely(fuel, area):
    """★ 檔案永遠 144 列，未來時段是 ','。整列跳過——不是 0 也不是 NULL。

    那些時間點根本還沒到，連「未報告」都算不上；寫成 0 會讓下午的用電看起來是零。
    """
    assert len({p.observed_at for p in fuel}) == 75
    assert len({p.observed_at for p in area}) == 75
    assert len(fuel) == 75 * 12
    assert len(area) == 75 * 4
    assert max(p.observed_at for p in fuel) == datetime(2026, 8, 5, 12, 20, tzinfo=TAIPEI)


def test_future_slots_produce_no_zero_points(fuel):
    """13:00 之後不該有任何一筆——連 mw=0 的都不行。"""
    after = datetime(2026, 8, 5, 13, 0, tzinfo=TAIPEI)
    assert [p for p in fuel if p.observed_at >= after] == []


# ── 7. genloadareaperc：拆成 area_gen / area_load ─────────────────

def test_areaperc_splits_into_gen_and_load():
    pts, base = P.parse_areaperc(read('genloadareaperc.csv'))
    assert base == BASE_DATE
    assert len(pts) == 8
    got = {(p.kind, p.label): p.mw for p in pts}
    # 2026-08-05 12:20,1304.1,1481.5,990.9,1076.7,1514.9,1230.1,29.2,50.8
    assert got[('area_gen', '北部')] == 13041.0
    assert got[('area_load', '北部')] == 14815.0
    assert got[('area_gen', '中部')] == 9909.0
    assert got[('area_load', '中部')] == 10767.0
    assert got[('area_gen', '南部')] == 15149.0
    assert got[('area_load', '南部')] == 12301.0
    assert got[('area_gen', '東部')] == 292.0
    assert got[('area_load', '東部')] == 508.0


def test_areaperc_flow_directions_match_reality():
    """驗證欄序的獨立方式：北發<用、中發<用、南發>用、東發<用（官網顯示的關係）。"""
    pts, _ = P.parse_areaperc(read('genloadareaperc.csv'))
    got = {(p.kind, p.label): p.mw for p in pts}
    assert got[('area_gen', '北部')] < got[('area_load', '北部')]
    assert got[('area_gen', '中部')] < got[('area_load', '中部')]
    assert got[('area_gen', '南部')] > got[('area_load', '南部')], '南部應是送出'
    assert got[('area_gen', '東部')] < got[('area_load', '東部')]


def test_areaperc_load_matches_area_curve(area):
    """★ 交叉驗收：占比檔的「用電」應與區域別曲線同時點吻合。"""
    pts, _ = P.parse_areaperc(read('genloadareaperc.csv'))
    t = datetime(2026, 8, 5, 12, 20, tzinfo=TAIPEI)
    load = {p.label: p.mw for p in pts if p.kind == 'area_load'}
    for label in ('北部', '中部', '南部', '東部'):
        assert load[label] == pytest.approx(value_at(area, label, t), abs=1.0)


# ── 8. parse_all 串接 ─────────────────────────────────────────────

def test_parse_all_uses_date_from_areaperc():
    """★ 日期基準取自帶完整時戳的 genloadareaperc，而不是「今天」——
    午夜前後執行才不會把昨天的資料標成今天。"""
    pts = P.parse_all(read('loadfueltype.csv'), read('loadareas.csv'),
                      read('genloadareaperc.csv'))
    assert {p.observed_at.date() for p in pts} == {BASE_DATE}
    assert {p.kind for p in pts} == {'fuel', 'area', 'area_gen', 'area_load'}
    assert len(pts) == 75 * 12 + 75 * 4 + 8


def test_parse_all_falls_back_to_taipei_today_without_areaperc():
    pts = P.parse_all(read('loadfueltype.csv'), read('loadareas.csv'), None)
    assert {p.observed_at.date() for p in pts} == {P.taipei_today()}


def test_parse_all_tolerates_missing_files():
    pts = P.parse_all(None, read('loadareas.csv'), read('genloadareaperc.csv'))
    assert {p.kind for p in pts} == {'area', 'area_gen', 'area_load'}
    assert P.cross_check(pts) is None, '缺能源別時無法交叉檢查'

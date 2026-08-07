"""正式執行路徑的解析編排：單檔隔離與跨午夜防線。"""
from datetime import date, datetime, timedelta
from pathlib import Path

from taipower_curve import parser as P

FIXTURES = Path(__file__).parent / 'fixtures'


def read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def good_bodies() -> dict[str, bytes]:
    return {n: read(n) for n in ('loadfueltype.csv', 'loadareas.csv',
                                 'genloadareaperc.csv', 'loadpara.json')}


# ── 單檔失敗不拖垮其他檔 ────────────────────────────────────────

def test_all_good_no_errors():
    points, errors = P.parse_files(good_bodies())
    assert errors == []
    assert {p.kind for p in points} == {'fuel', 'area', 'area_gen',
                                        'area_load', 'capacity'}


def test_broken_loadpara_does_not_kill_curves():
    """★ 這就是改成逐檔隔離的理由：loadpara 改版不可以讓曲線陪葬。"""
    bodies = good_bodies()
    bodies['loadpara.json'] = b'{"records": [{"what": "1"}]}'
    points, errors = P.parse_files(bodies)
    kinds = {p.kind for p in points}
    assert 'fuel' in kinds and 'area' in kinds, '曲線必須還在'
    assert 'capacity' not in kinds, '壞掉的 loadpara 不可以產生點'
    assert len(errors) == 1 and 'loadpara' in errors[0]


def test_broken_perc_does_not_kill_curves_and_falls_back_to_today():
    bodies = good_bodies()
    bodies['genloadareaperc.csv'] = b'2026-08-06 14:10,1.0,2.0\n'   # 欄數不符
    points, errors = P.parse_files(bodies)
    kinds = {p.kind for p in points}
    assert 'fuel' in kinds and 'area' in kinds
    assert 'area_gen' not in kinds
    assert len(errors) == 1 and 'genloadareaperc' in errors[0]
    # 日期基準退回台北今天
    assert {p.observed_at.date() for p in points
            if p.kind == 'fuel'} == {P.taipei_today()}


def test_broken_fuel_does_not_kill_area():
    bodies = good_bodies()
    bodies['loadfueltype.csv'] = b'00:10,1.0,2.0\n'                 # 欄數不符
    points, errors = P.parse_files(bodies)
    kinds = {p.kind for p in points}
    assert 'area' in kinds, '區域別曲線必須還在'
    assert 'fuel' not in kinds
    assert 'capacity' not in kinds, '沒有 fuel 就沒有可信時戳，capacity 不該出現'
    assert len(errors) == 1 and 'loadfueltype' in errors[0]


def test_two_broken_files_two_errors():
    bodies = good_bodies()
    bodies['loadpara.json'] = b'not json at all'
    bodies['loadareas.csv'] = b'00:10,1.0\n'
    points, errors = P.parse_files(bodies)
    assert {p.kind for p in points} >= {'fuel', 'area_gen', 'area_load'}
    assert len(errors) == 2


def test_parse_all_strict_still_raises():
    """嚴格版（測試/驗收用）維持原語意：任一支壞就丟例外。"""
    import pytest
    bodies = good_bodies()
    with pytest.raises(P.ParseError, match='loadpara'):
        P.parse_all(bodies['loadfueltype.csv'], bodies['loadareas.csv'],
                    bodies['genloadareaperc.csv'], b'broken')


# ── 跨午夜防線 ──────────────────────────────────────────────────

def test_normal_data_has_no_future_points():
    points, _ = P.parse_files(good_bodies())
    # fixture 是 2026-08-06 14:10 抓的，用當時的「現在」來看必為空
    now = datetime(2026, 8, 6, 14, 20, tzinfo=P.TAIPEI)
    assert P.find_future_points(points, now) == []


def test_midnight_mislabel_is_caught():
    """★ 重現事故：舊日滿檔被換日後的 perc 日期標成新一天 → 幾乎全是未來點。"""
    stale_full_day = read('loadfueltype.csv')       # 舊日資料（75 個時點）
    tomorrow = date(2026, 8, 7)                     # perc 已換日
    points = P.parse_curve(stale_full_day, 'fuel', P.FUEL_COLUMNS, tomorrow)
    now = datetime(2026, 8, 7, 0, 0, 30, tzinfo=P.TAIPEI)   # 剛過午夜
    future = P.find_future_points(points, now)
    assert future, '整天份的假未來資料必須被抓到'
    # 00:00/00:10/00:20 在容忍值內抓不到（會被下次 upsert 蓋掉），其餘都要中
    assert len(future) >= 70 * 12


def test_tolerance_allows_publication_jitter():
    """容忍值要放得過正常的發布延遲（7–11 分鐘），不能誤殺剛出爐的點。"""
    points, _ = P.parse_files(good_bodies())
    latest = max(p.observed_at for p in points)
    # 模擬「資料點 14:10 在 14:05 就抓到了」這種時鐘誤差＋提早發布的極端情況
    now = latest - timedelta(minutes=5)
    assert P.find_future_points(points, now) == [], '正常發布不可以被誤殺'

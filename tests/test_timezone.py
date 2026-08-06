"""時區處理的測試。

兩件事各自獨立、都要成立：
  1. 寫進資料庫的每個 datetime 都是 **aware** 的（沒有 naive 漏出去）
  2. 時區用的是 **IANA 時區** Asia/Taipei，不是硬編的 +08:00 固定偏移
"""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from taipower_curve import archive
from taipower_curve import parser as P

FIXTURES = Path(__file__).parent / 'fixtures'


def read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def all_points():
    return P.parse_all(read('loadfueltype.csv'), read('loadareas.csv'),
                       read('genloadareaperc.csv'), read('loadpara.json'))


# ── 1. 用的是 IANA 時區，不是硬編偏移 ──────────────────────────

def test_taipei_is_an_iana_zone_not_a_fixed_offset():
    """★★ 這條是重點：不可以改回 timezone(timedelta(hours=8))。

    固定偏移說的是「某個剛好 +8 的偏移」，IANA 時區說的是「台北這個地方」。
    讓 tz 資料庫去回答偏移是多少，不要自己硬編。
    """
    assert isinstance(P.TAIPEI, ZoneInfo), \
        f'TAIPEI 應該是 ZoneInfo，不是 {type(P.TAIPEI).__name__}'
    assert P.TAIPEI.key == 'Asia/Taipei'
    assert not isinstance(P.TAIPEI, timezone), '不可以是固定偏移的 timezone 物件'


def test_source_has_no_hardcoded_offset():
    """程式碼裡不該再出現 timezone(timedelta(hours=8)) 這種寫法。"""
    root = Path(__file__).resolve().parent.parent
    for mod in ('parser.py', 'archive.py', 'db.py', 'fetch.py', 'telemetry.py'):
        src = (root / 'taipower_curve' / mod).read_text(encoding='utf-8')
        code = '\n'.join(l for l in src.splitlines()
                         if not l.lstrip().startswith('#'))
        assert 'timedelta(hours=8)' not in code, f'{mod} 有硬編的 +08:00 偏移'


def test_iana_zone_gives_correct_offset_today(all_points):
    """台灣現行是 UTC+8 且無日光節約，所以偏移應該是 +8。

    這條驗的是「用 IANA 時區算出來的結果仍然正確」，不是「偏移永遠是 8」。
    """
    for p in all_points[:50]:
        assert p.observed_at.utcoffset() == timedelta(hours=8)


def test_astimezone_does_not_depend_on_machine_locale():
    """★ 歸檔目錄名要明示 Asia/Taipei，不可跟著系統時區跑。"""
    root = Path(__file__).resolve().parent.parent
    src = (root / 'taipower_curve' / 'archive.py').read_text(encoding='utf-8')
    assert '.astimezone(TAIPEI)' in src
    assert '.astimezone()' not in src, 'astimezone() 不帶參數會跟著機器的系統時區'


# ── 2. 沒有 naive datetime 會寫進資料庫 ────────────────────────

def test_every_point_observed_at_is_aware(all_points):
    """★ naive datetime 進到 timestamptz 欄位，資料庫會用連線的 TimeZone 設定
    去猜——猜錯就整批偏移，而且畫面看起來完全正常。"""
    assert all_points
    for p in all_points:
        assert p.observed_at.tzinfo is not None, f'{p} 的 observed_at 是 naive'
        assert p.observed_at.utcoffset() is not None


def test_every_kind_is_aware(all_points):
    """四種 kind 走的是不同的解析路徑，每一條都要驗到。"""
    kinds = {p.kind for p in all_points}
    assert kinds == {'fuel', 'area', 'area_gen', 'area_load', 'capacity'}
    for kind in kinds:
        pts = [p for p in all_points if p.kind == kind]
        assert pts
        assert all(p.observed_at.utcoffset() is not None for p in pts), \
            f'kind={kind} 有 naive 的 observed_at'


def test_parse_time_returns_aware():
    for raw in ('00', '00:10', '23:50'):
        assert P.parse_time(raw, date(2026, 8, 5)).utcoffset() is not None


def test_full_timestamp_parse_returns_aware():
    pts, _ = P.parse_areaperc(read('genloadareaperc.csv'))
    assert all(p.observed_at.utcoffset() is not None for p in pts)


def test_taipei_today_uses_taipei_not_utc():
    """★ 「今日」要用台北的今天。用 UTC 日期會在早上 8 點前錯一天。"""
    assert P.taipei_today() == datetime.now(P.TAIPEI).date()


# ── 3. 正確性：台北時間換算成 UTC 要對 ─────────────────────────

def test_taipei_midnight_is_previous_day_in_utc():
    """台北 00:00 存進 timestamptz，讀出來的 UTC 應該是前一天 16:00。"""
    t = P.parse_time('00', date(2026, 8, 5))
    assert t.astimezone(timezone.utc) == datetime(2026, 8, 4, 16, 0,
                                                  tzinfo=timezone.utc)


def test_same_instant_regardless_of_representation():
    """改用 ZoneInfo 之後，跟舊的固定偏移表示法仍是同一個瞬間。

    （所以既有測試不會因為這次改動而失效——它們比的是瞬間不是表示法。）
    """
    iana = datetime(2026, 8, 5, 12, 20, tzinfo=ZoneInfo('Asia/Taipei'))
    fixed = datetime(2026, 8, 5, 12, 20, tzinfo=timezone(timedelta(hours=8)))
    assert iana == fixed
    assert hash(iana) == hash(fixed)


def test_archive_prune_uses_taipei_date():
    root = Path(__file__).resolve().parent.parent
    src = (root / 'taipower_curve' / 'archive.py').read_text(encoding='utf-8')
    assert 'datetime.now(TAIPEI).date()' in src
    assert 'datetime.now().date()' not in src, '裸的 now() 會用系統時區'

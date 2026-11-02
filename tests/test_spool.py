"""離線暫存（2026-11-02）：資料庫連不上時先存本機，連得上再回傳。

★★ 這組測試釘住三件事：
   1. 連不上資料庫時資料**不可以丟**——存下來，而且這次要報失敗（告警要燒）。
   2. 回傳時**資料庫已經有的值不覆寫**，只補沒有的列與 mw 是 NULL 的列。
      暫存是舊的抓取，用一般的 DO UPDATE 回傳會把台電後來的更正蓋回舊值，
      而且畫面看起來完全正常。
   3. 暫存是空的時候一次資料庫呼叫都不做（待命那條測試靠這個）。
"""
from datetime import datetime, timedelta
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from taipower_curve import db, spool, parser as P  # noqa: E402

import run_once  # noqa: E402

TPE = P.TAIPEI
NOW = datetime(2026, 11, 2, 15, 56, tzinfo=TPE)


def _run_kw(fetched_at, note='x'):
    return dict(fetched_at=fetched_at, status='ok', record_count=2,
                data_timestamp=fetched_at, span_lo=fetched_at, span_hi=fetched_at,
                http_status=200, duration_ms=10, note=note,
                raw_uri=None, raw_sha256=None)


def _pts(t, mw=1.0):
    return [P.Point(t, 'fuel', '燃氣', mw), P.Point(t, 'area', '北部', None)]


class FakeDB:
    DatabaseError = RuntimeError

    def __init__(self, die_after=None):
        self.calls, self.runs, self.die_after = [], [], die_after

    def fill_missing_points(self, engine, points, fetched_at):
        if self.die_after is not None and len(self.runs) >= self.die_after:
            raise self.DatabaseError('斷線')
        self.calls.append(('fill', fetched_at))
        return len(points) - 1, 0          # 假裝有一列早已在庫

    def upsert_points(self, engine, points, fetched_at):
        self.calls.append(('upsert', fetched_at))
        return len(points), 0

    def insert_fetch_run(self, engine, **kw):
        self.calls.append(('run', kw['fetched_at']))
        self.runs.append(kw)


# ── 存與讀 ──────────────────────────────────────────────────────

def test_round_trip_keeps_aware_datetimes_and_none():
    t = NOW - timedelta(hours=3)
    spool.save(_run_kw(t), _pts(t))
    run, pts = spool._load(spool.pending()[0])
    assert run['fetched_at'] == t and run['fetched_at'].tzinfo is not None
    assert pts[0].observed_at == t and pts[0].observed_at.tzinfo is not None
    assert pts[1].mw is None, '★ 未知≠零：NULL 存回來還是 None'
    assert pts[0].mw == 1.0


def test_empty_spool_makes_no_db_calls():
    d = FakeDB()
    assert spool.flush(d, 'E', NOW) is True
    assert d.calls == []


# ── 回傳 ────────────────────────────────────────────────────────

def test_flush_uses_fill_only_oldest_first_and_deletes():
    """★★ 回傳走 fill_missing_points（只補缺），不是 upsert_points（會覆寫）。"""
    t1, t2 = NOW - timedelta(hours=5), NOW - timedelta(hours=2)
    spool.save(_run_kw(t2), _pts(t2))
    spool.save(_run_kw(t1), _pts(t1))
    d = FakeDB()

    assert spool.flush(d, 'E', NOW) is True
    assert [c[0] for c in d.calls] == ['fill', 'run', 'fill', 'run'], d.calls
    assert [c[1] for c in d.calls if c[0] == 'fill'] == [t1, t2], '要由舊到新'
    assert spool.pending() == []
    assert d.runs[0]['record_count'] == 1, 'record_count 記真的補進去幾列'
    assert '已在庫 1 點未覆寫' in d.runs[0]['note']
    assert '回傳' in d.runs[0]['note']
    assert d.runs[0]['fetched_at'] == t1, 'fetch_run 保留原始抓取時間'


def test_flush_stops_on_db_error_and_keeps_the_rest():
    for h in (5, 4, 3):
        t = NOW - timedelta(hours=h)
        spool.save(_run_kw(t), _pts(t))
    d = FakeDB(die_after=1)

    assert spool.flush(d, 'E', NOW) is False
    assert len(spool.pending()) == 2, '斷線之後的暫存不可以刪'


def test_unreadable_file_is_set_aside_not_deleted():
    spool.SPOOL_DIR.mkdir(parents=True)
    (spool.SPOOL_DIR / '20261101T000000Z.json').write_text('{壞', encoding='utf-8')
    assert spool.flush(FakeDB(), 'E', NOW) is True
    assert (spool.SPOOL_DIR / '20261101T000000Z.bad').exists()


# ── 補缺 SQL 的語意（用 SQLite 實跑同一段 SQL 文字）──────────────
#   這個專案沒有測試用的 PostgreSQL；ON CONFLICT ... DO UPDATE ... WHERE 與
#   RETURNING 兩邊語意相同，所以拿 SQLite 跑「釘的是 WHERE 條件有沒有被改掉」。

def _sqlite_fill(existing, incoming):
    con = sqlite3.connect(':memory:')
    con.execute('CREATE TABLE monitor_power_load_curve (observed_at, kind, label,'
                ' mw, parser_version, fetched_at,'
                ' PRIMARY KEY (observed_at, kind, label))')
    con.executemany('INSERT INTO monitor_power_load_curve VALUES (?,?,?,?,?,?)',
                    existing)
    sql = db._FILL_CURVE.replace('%s', '(?,?,?,?,?,?)')
    written = sum(len(con.execute(sql, row).fetchall()) for row in incoming)
    rows = {r[:3]: r[3] for r in
            con.execute('SELECT * FROM monitor_power_load_curve')}
    return written, rows


def test_fill_sql_never_overwrites_existing_values():
    """★★ 使用者要求（2026-11-02）：已經有的部分不覆寫，沒有或有缺漏的才寫。"""
    existing = [('10:00', 'fuel', '燃氣', 999.0, 'v', 'new'),   # 已有值（台電更正後）
                ('10:00', 'fuel', '風力', None, 'v', 'new')]    # 缺漏（未報告）
    incoming = [('10:00', 'fuel', '燃氣', 111.0, 'v', 'old'),   # 舊值 → 不可蓋
                ('10:00', 'fuel', '風力', 50.0, 'v', 'old'),    # 補缺漏
                ('10:10', 'fuel', '燃氣', 222.0, 'v', 'old')]   # 庫裡沒有 → 寫入
    written, rows = _sqlite_fill(existing, incoming)
    assert rows[('10:00', 'fuel', '燃氣')] == 999.0, '★ 已有的值被舊暫存蓋掉了'
    assert rows[('10:00', 'fuel', '風力')] == 50.0, '缺漏（NULL）要補上'
    assert rows[('10:10', 'fuel', '燃氣')] == 222.0, '沒有的列要寫入'
    assert written == 2, '寫入筆數只算真的寫的'


def test_fill_sql_does_not_wash_a_value_back_to_null():
    written, rows = _sqlite_fill([('10:00', 'area', '北部', 1434.3, 'v', 'new')],
                                 [('10:00', 'area', '北部', None, 'v', 'old')])
    assert rows[('10:00', 'area', '北部')] == 1434.3 and written == 0


def test_normal_upsert_still_overwrites():
    """一般執行照舊 DO UPDATE、沒有 WHERE——台電的更正要跟得上（CLAUDE.md）。"""
    sql = ' '.join(db._UPSERT_CURVE.split())
    assert 'DO UPDATE' in sql and 'WHERE' not in sql, sql


# ── run_once 的整條路 ──────────────────────────────────────────

class _Result:
    errors: dict = {}
    http_status = 200

    def __init__(self, empty=False):
        # empty＝四支全抓失敗（例如 CloudFront 403），跟真的 fetch_all 一樣帶 errors
        self.errors = ({n: f'{n}: HTTP 403' for n in ('loadfueltype.csv',
                       'loadareas.csv')} if empty else {})
        fix = Path(__file__).parent / 'fixtures' / '20260829'
        self.bodies = {} if empty else {
            n: (fix / n).read_bytes() for n in
            ('loadfueltype.csv', 'loadareas.csv', 'genloadareaperc.csv',
             'unitdata.json')}


class RunDB(FakeDB):
    backup_marker = staticmethod(db.backup_marker)

    def __init__(self, upsert_dies=False, run_dies=False, ask_dies=False):
        super().__init__()
        self.upsert_dies, self.run_dies, self.ask_dies = upsert_dies, run_dies, ask_dies

    def make_engine(self, cfg):
        return 'E'

    def last_success_at(self, engine, own_marker):
        self.calls.append(('ask', None))
        if self.ask_dies:
            raise self.DatabaseError('連不上')
        return None

    def upsert_points(self, engine, points, fetched_at):
        if self.upsert_dies:
            raise self.DatabaseError('連不上')
        return super().upsert_points(engine, points, fetched_at)

    def insert_fetch_run(self, engine, **kw):
        if self.run_dies:
            raise self.DatabaseError('連不上')
        super().insert_fetch_run(engine, **kw)


def _patch(monkeypatch, d, empty=False):
    fetched = []

    def fake_fetch(*a, **k):
        fetched.append(1)
        return _Result(empty)
    monkeypatch.setattr(run_once, 'db', d)
    monkeypatch.setattr(run_once.fetch, 'fetch_all', fake_fetch)
    monkeypatch.setattr(run_once.archive, 'archive_run', lambda *a, **k: (None, None))
    monkeypatch.setattr(run_once.archive, 'prune', lambda *a, **k: 0)
    return fetched


NORMAL = {'mode': 'normal', 'crawler': {'user_agent': 'x'},
          'monitoring': {'instance_id': 'win-relay-02'}}
BACKUP = {**NORMAL, 'mode': 'backup'}
RUN_AT = datetime(2026, 8, 29, 20, 55, tzinfo=TPE)   # fixture 那一輪


def test_db_down_on_write_spools_everything_and_reports_error(monkeypatch):
    _patch(monkeypatch, RunDB(upsert_dies=True))
    status, items, errors = run_once._run(NORMAL, RUN_AT, 0.0)

    assert status == 'error' and items == 0 and errors >= 1, \
        '★ 暫存不是「沒事」——資料沒送達，告警要燒'
    [f] = spool.pending()
    run, pts = spool._load(f)
    assert len(pts) > 100, '這次抓到的點要全部存下來'
    assert run['status'] == 'ok' and run['record_count'] == len(pts), \
        'fetch_run 記的是「抓取」的結果，抓取本身是成功的'


def test_fetch_run_failure_alone_spools_the_run_row(monkeypatch):
    _patch(monkeypatch, RunDB(run_dies=True))
    status, _, _ = run_once._run(NORMAL, RUN_AT, 0.0)
    [f] = spool.pending()
    _, pts = spool._load(f)
    assert pts == [], '資料已寫進去了，不要重送'
    assert status == 'error'


def test_pending_spool_is_flushed_before_this_runs_write(monkeypatch):
    """★ 舊暫存先補缺，這次新抓的再 upsert 蓋過——最後留下的一定是最新的。"""
    old = RUN_AT - timedelta(hours=2)
    spool.save(_run_kw(old), _pts(old))
    d = RunDB()
    _patch(monkeypatch, d)
    run_once._run(NORMAL, RUN_AT, 0.0)

    kinds = [c[0] for c in d.calls]
    assert kinds.index('fill') < kinds.index('upsert'), kinds
    assert spool.pending() == []


def test_backup_with_db_unknown_fetches_and_spools(monkeypatch):
    """★★ 2026-11-02 反轉：備援問不到資料庫時**照常抓、離線暫存**。

    原本是「不抓」，理由是「抓回來也寫不進去」——有了暫存之後不成立了。
    不抓的話，主端也死掉的日子跨過午夜的點會永久遺失。
    """
    d = RunDB(ask_dies=True)
    fetched = _patch(monkeypatch, d)
    status, _, _ = run_once._run(BACKUP, RUN_AT, 0.0)

    assert fetched == [1]
    assert status == 'error'
    assert not any(c[0] in ('upsert', 'run') for c in d.calls), \
        '已知連不上就別再各等一次逾時'
    [f] = spool.pending()
    run, pts = spool._load(f)
    assert pts
    assert run['note'].startswith('備援(win-relay-02)'), run['note']
    assert '主端狀態未知' in run['note']


def test_backup_offline_with_nothing_fetched_does_not_spool(monkeypatch):
    """這台自己斷網（DB 與台電都連不上）不是「這個來源」的事，不塞 error 列。"""
    _patch(monkeypatch, RunDB(ask_dies=True), empty=True)
    status, _, _ = run_once._run(BACKUP, RUN_AT, 0.0)
    assert status == 'error'
    assert spool.pending() == []


def test_normal_mode_db_down_with_nothing_fetched_still_spools_the_run(monkeypatch):
    """主端的失敗紀錄（標準 #5）不可以因為連不上資料庫就消失。"""
    _patch(monkeypatch, RunDB(run_dies=True), empty=True)
    run_once._run(NORMAL, RUN_AT, 0.0)
    [f] = spool.pending()
    run, _ = spool._load(f)
    assert run['status'] == 'error' and run['record_count'] is None


def test_backup_takeover_with_fetch_run_failure_still_spools_the_run(monkeypatch):
    """備援接手、資料寫進去了但 fetch_run 那一下斷線 → 那列健康紀錄仍要暫存。

    「不暫存」只限於「備援什麼都沒抓到」（這台自己斷網）那一種。
    """
    _patch(monkeypatch, RunDB(run_dies=True))
    run_once._run(BACKUP, RUN_AT, 0.0)
    [f] = spool.pending()
    run, pts = spool._load(f)
    assert pts == [] and run['note'].startswith('備援(win-relay-02)')


def test_a_stuck_spool_file_does_not_stop_this_run_from_writing(monkeypatch):
    """★★ 回傳卡住 ≠ 資料庫連不上。

    某個暫存檔自己寫不進去（約束、權限）時，這次的抓取仍要照常寫進庫。
    若把回傳失敗當成斷線，這台會從此每次都只暫存不寫庫，而 log 說「連不上」。
    """
    old = RUN_AT - timedelta(hours=2)
    spool.save(_run_kw(old), _pts(old))

    class StuckFill(RunDB):
        def fill_missing_points(self, engine, points, fetched_at):
            raise self.DatabaseError('CHECK 約束')
    d = StuckFill()
    _patch(monkeypatch, d)
    status, items, _ = run_once._run(NORMAL, RUN_AT, 0.0)

    kinds = [c[0] for c in d.calls]
    assert 'upsert' in kinds and 'run' in kinds, kinds
    assert items > 0, "這次的資料要真的寫進庫"
    assert len(spool.pending()) == 1, '卡住的那個留著，這次自己的不必暫存'

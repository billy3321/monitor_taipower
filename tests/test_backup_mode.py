"""備援模式：主端還活著就不要跟它搶，死了才接手（2026-09-12）。

★★ 這組測試釘住的是**兩個方向都會出錯**的判斷：
   - 判太鬆（主端活著也接手）→ 兩台一起抓、一起寫，健康頁上互相蓋來蓋去，
     主端哪天真的死了**看起來完全正常**，沒有人會發現。
   - 判太緊（主端死了也不接手）→ 備援等於沒裝。而台電的檔 00:00 換日重置，
     沒接上的那幾個小時是**永久遺失**，事後補不回來。

★ 待命那一次的形狀也要釘：不抓、不寫、不記 fetch_run，但**照推遙測**
  （而且推 last_success）。少了最後這一條，一台長期待命的備援自己死掉時
  沒有任何序列會過期，存活告警對空向量求值、永遠不燒。
"""
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from taipower_curve import config as cfgmod, db, parser as P, telemetry  # noqa: E402

import run_once  # noqa: E402

TPE = P.TAIPEI
NOW = datetime(2026, 9, 12, 15, 20, tzinfo=TPE)

CFG = {
    'mode': 'backup',
    'crawler': {'user_agent': 'x'},
    'monitoring': {'instance_id': 'win-relay-02',
                   'pushgateway': {'enabled': False}},
}


# ── 判斷本身（純函式，不碰資料庫）────────────────────────────────

def test_recent_success_means_stand_down():
    """主端 :55 跑、備援 :56 跑——健康時兩者相隔 1 分鐘，落在窗內。"""
    assert run_once.should_stand_down(NOW - timedelta(minutes=1), NOW) is True


def test_stale_success_means_take_over():
    """主端睡著了（實際發生過：Mac 休眠 9 小時）——備援要接手。"""
    assert run_once.should_stand_down(NOW - timedelta(hours=9), NOW) is False


def test_never_succeeded_means_take_over():
    """★★ None 是「從來沒成功過」，不是「剛剛成功」。

    反過來寫的話，第一次部署的備援會永遠待命，而且待命得看起來完全正常。
    """
    assert run_once.should_stand_down(None, NOW) is False


def test_window_boundary_is_exclusive():
    """剛好等於窗長＝已經超過，要接手。"""
    assert run_once.should_stand_down(NOW - run_once.BACKUP_WINDOW, NOW) is False
    assert run_once.should_stand_down(
        NOW - run_once.BACKUP_WINDOW + timedelta(seconds=1), NOW) is True


def test_window_is_boxed_in_on_both_sides():
    """★★ 窗長被排程夾在中間，兩邊都越界不得：

        排程錯開（1 分鐘）< BACKUP_WINDOW < 執行間隔（60 分鐘）

    - 小於左邊：主端 :55 成功、備援 :56 來看，卻判定它死了 → 兩台搶著抓。
    - 大於右邊：主端死了，備援每次來看都還在窗內 → **永遠不接手**，
      而且待命得看起來完全正常。

    改排程沒回來改這個常數（或反過來）就會靜靜地掉進其中一邊。
    """
    assert timedelta(minutes=1) < run_once.BACKUP_WINDOW < timedelta(minutes=60)


# ── 待命那一次的形狀 ────────────────────────────────────────────

class _SpyDB:
    """把資料庫呼叫記下來。DatabaseError 要有，run_once 會 except 它。"""
    DatabaseError = RuntimeError

    def __init__(self, last_ok):
        self.last_ok = last_ok
        self.calls = []
        self.run_kw = None
        self.own_marker = None

    def make_engine(self, cfg):
        return 'FAKE-ENGINE'

    def last_success_at(self, engine, own_marker):
        self.calls.append('last_success_at')
        self.own_marker = own_marker
        return self.last_ok

    @staticmethod
    def backup_marker(instance_id):
        return db.backup_marker(instance_id)

    def upsert_points(self, engine, points, fetched_at):
        self.calls.append('upsert_points')
        return len(points), 0

    def insert_fetch_run(self, engine, **kw):
        self.calls.append('insert_fetch_run')
        self.run_kw = kw


class _FakeResult:
    errors: dict = {}
    http_status = 200

    @property
    def bodies(self):
        fix = Path(__file__).parent / 'fixtures' / '20260829'
        return {n: (fix / n).read_bytes() for n in
                ('loadfueltype.csv', 'loadareas.csv', 'genloadareaperc.csv',
                 'unitdata.json')}


def _patch(monkeypatch, spydb, fetched):
    def fake_fetch_all(*a, **k):
        fetched.append(1)
        return _FakeResult()

    monkeypatch.setattr(run_once, 'db', spydb)
    monkeypatch.setattr(run_once.fetch, 'fetch_all', fake_fetch_all)
    monkeypatch.setattr(run_once.archive, 'archive_run',
                        lambda *a, **k: (None, None))
    monkeypatch.setattr(run_once.archive, 'prune', lambda *a, **k: 0)


def test_standby_touches_nothing(monkeypatch):
    """★★ 待命＝一個請求都不發、一列都不寫、連 fetch_run 都不記。

    fetch_run 記的是「這個來源被抓了一次」——待命沒有抓，寫一列 ok 是謊，
    寫一列 error 也是謊。兩種說法都會讓健康頁講錯話。
    """
    spy, fetched = _SpyDB(NOW - timedelta(minutes=1)), []
    _patch(monkeypatch, spy, fetched)

    status, items, errors = run_once._run(CFG, NOW, 0.0)

    assert status == 'standby'
    assert (items, errors) == (0, 0)
    assert fetched == [], '★ 待命不可以去抓台電——爬取自律'
    assert spy.calls == ['last_success_at'], \
        f'待命只該查一次，實際呼叫了 {spy.calls}'


def test_standby_exit_code_is_zero(monkeypatch):
    """待命是**成功的結果**。回非零會讓排程器與人都以為出事了。"""
    spy = _SpyDB(datetime.now(timezone.utc) - timedelta(minutes=5))
    monkeypatch.setattr(run_once, 'db', spy)
    monkeypatch.setattr(run_once.cfgmod, 'load', lambda *a, **k: CFG)
    assert run_once.main([]) == 0


# ── 接手那一次的形狀 ────────────────────────────────────────────

def test_takeover_fetches_writes_and_says_who(monkeypatch):
    """主端死了 → 照常抓寫，而且 note 要留下是哪一台接手的。

    ★ monitor_fetch_run 沒有「誰寫的」欄位，備援寫的列跟主端寫的列長得
      一模一樣。不標記的話，事後查「那天到底是誰在寫」根本無從查起。
    """
    spy, fetched = _SpyDB(NOW - timedelta(hours=9)), []
    _patch(monkeypatch, spy, fetched)

    status, items, errors = run_once._run(CFG, NOW, 0.0)

    assert fetched == [1], '主端死了就要真的去抓'
    assert 'upsert_points' in spy.calls and 'insert_fetch_run' in spy.calls
    assert items > 0
    assert spy.run_kw['note'].startswith('備援(win-relay-02)'), spy.run_kw['note']


def test_own_rows_are_excluded_from_the_liveness_query():
    """★★ 23:59 那一次的命脈：查詢必須把**自己寫的列**排除掉。

    主端死掉的日子，備援 23:56 接手成功、寫下一列；三分鐘後的 23:59
    （專門用來收當日最後那個 23:50 點的加班場次）如果把自己 23:56 那列
    也算成「最近有人成功」，就會待命不動——於是**每天固定少掉 23:50
    前後那幾個點**，而且圖上看起來只像那時候沒用電。那正是 CLAUDE.md
    排程那一節花了一整段消滅的形狀。

    ★ 這是**靜態檢查 SQL 文字**（同 test_side_fallback 對 upsert 的做法）：
      這個專案沒有測試用的 PostgreSQL，而這條釘的是「語意沒有被改掉」。
    """
    sql = ' '.join(db._LAST_SUCCESS.text.split())
    assert 'note NOT LIKE :own_marker' in sql, sql
    assert 'note IS NULL OR' in sql, 'note 是 NULL 的列（別人寫的）不可以被濾掉'


def test_the_marker_written_is_the_marker_filtered(monkeypatch):
    """★ 查詢用的記號與寫進 note 的記號必須是同一個字串。

    兩邊漂移的話，備援會把自己的列當成別人的 → 23:59 永遠待命；
    而且**看起來完全正常**。所以只有 db.backup_marker() 一個定義處。
    """
    spy, fetched = _SpyDB(NOW - timedelta(hours=9)), []
    _patch(monkeypatch, spy, fetched)

    run_once._run(CFG, NOW, 0.0)

    assert spy.own_marker == '備援(win-relay-02)', spy.own_marker
    assert spy.run_kw['note'].startswith(spy.own_marker), spy.run_kw['note']


def test_normal_mode_never_asks_the_database_first(monkeypatch):
    """normal 模式不該多跑那一趟查詢——主端就是主端，不用問誰活著。"""
    spy, fetched = _SpyDB(NOW), []
    _patch(monkeypatch, spy, fetched)

    run_once._run({**CFG, 'mode': 'normal'}, NOW, 0.0)

    assert 'last_success_at' not in spy.calls
    assert fetched == [1]


def test_backup_flag_overrides_config(monkeypatch):
    """--backup 旗標要蓋過 config 的 mode（手動在別台補跑時用得到）。"""
    spy, fetched = _SpyDB(NOW - timedelta(minutes=5)), []
    _patch(monkeypatch, spy, fetched)

    status, _, _ = run_once._run({**CFG, 'mode': 'normal'}, NOW, 0.0,
                                 force_backup=True)

    assert status == 'standby' and fetched == []


def test_db_unreachable_does_not_fail_open(monkeypatch):
    """★★ 問不到資料庫就**不要去抓**。

    連「該不該接手」都答不出來的時候，抓回來也寫不進去——那只是拿台電的
    頻寬去證明我們的資料庫壞了。而且這一類失敗（DB 端）跟「抓不到台電」
    要修的東西完全不同，混在一起會叫人去修錯的東西。
    """
    fetched: list = []

    class Dead(_SpyDB):
        def last_success_at(self, engine, own_marker):
            raise self.DatabaseError('連不上')

    _patch(monkeypatch, Dead(None), fetched)

    with pytest.raises(RuntimeError):
        run_once._run(CFG, NOW, 0.0)
    assert fetched == [], '★ 不可以 fail-open 去抓'


# ── config 的 mode ─────────────────────────────────────────────

def test_unknown_mode_raises_instead_of_silently_running_normal():
    """★ mode: backupp 打錯字不可以默默變成 normal——那台機器會以為
    自己在待命，實際上每小時跟主端搶著抓，而且**看起來完全正常**。"""
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.mode({'mode': 'backupp'})


def test_missing_mode_defaults_to_normal():
    """既有的 config.yml 沒有 mode 這一節，行為必須完全不變。"""
    assert cfgmod.mode({}) == 'normal'
    assert cfgmod.mode({'mode': None}) == 'normal'


# ── 待命時的遙測 ───────────────────────────────────────────────

TCFG = {'monitoring': {'pushgateway': {'enabled': True, 'url': 'http://x:1'},
                       'instance_id': 'win-relay-02'}}


def _standby_metrics(monkeypatch):
    calls = []

    def fake(url, job, registry, grouping_key, timeout):
        calls.append(registry)

    monkeypatch.setattr(telemetry, 'pushadd_to_gateway', fake)
    telemetry.push(TCFG, run_ts=9000.0, status='standby', items=0,
                   errors=0, duration=0.2)
    return {s.name: s.value
            for m in calls[0].collect() for s in m.samples}


def test_standby_telemetry_keeps_the_alarm_alive(monkeypatch):
    """★★ 待命要推 last_success。

    不推的話，一台長期待命（＝主端一直很健康）的備援根本不會有
    last_success 這條序列，而存活告警是
    time() - scrapy_last_success_timestamp_seconds > 門檻——
    對空向量求值**永遠不會燒**。備援機器自己死掉時沒有人知道，
    跟 pushadd／push 那條教訓是同一個形狀。
    """
    m = _standby_metrics(monkeypatch)
    assert m['scrapy_last_success_timestamp_seconds'] == 9000.0
    assert m['scrapy_last_run_timestamp_seconds'] == 9000.0
    assert m['scrapy_max_stale_seconds'] == telemetry.MAX_STALE_SECONDS


def test_standby_pushes_neither_item_metric(monkeypatch):
    """★★ 待命這次沒有去抓，所以筆數既不是 0（來源確實沒東西）
    也不是「未知」（抓失敗了）——兩個指標都不推。硬推哪一個都是說謊，
    而且會讓判讀的人以為備援每小時都在抓失敗。"""
    m = _standby_metrics(monkeypatch)
    assert 'scrapy_items_unknown' not in m, '待命不是「抓失敗」'
    assert 'scrapy_items_scraped' not in m, '待命不是「抓到 0 筆」'

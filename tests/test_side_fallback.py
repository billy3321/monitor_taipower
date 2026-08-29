"""一側壞掉時只丟那一側，而且寫進去的東西會自己修正（2026-08-29）。

★★ 這組測試存在的理由是一次真實的資料遺失：2026-08-28 08:00 起台電自己的
   兩份數字對不齊（能源別合計比區域別低 0~336 MW），而當時的交叉檢查是
   **整批不寫**，於是那天 08:00~23:50 共 16 小時永久遺失
   （來源當日歸零、沒有歷史檔）。

   一邊有問題不該讓另一邊陪葬，而「先寫進去」之所以安全，是因為
   upsert 會在下一次跑的時候把整天重寫一遍。
"""
from datetime import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from taipower_curve import db, parser as P


def test_upsert_overwrites_so_upstream_corrections_propagate():
    """★★ 「照樣寫入、之後會修正」這句話的**全部依據**就在這段 SQL 裡。

    每次執行都會重寫整天（來源是當日累積檔），而 upsert 是
    ON CONFLICT DO UPDATE——所以台電哪天更正了數字，我們下一次跑就跟著改。
    改成 DO NOTHING 的話，第一次寫進去的錯值會**永遠凍在那裡**，
    而且畫面上完全看不出來。

    ★ 這是**靜態檢查 SQL 文字**，不是真的跑一次資料庫來回：
      upsert 走 psycopg2 的 execute_values，SQLite 測不了，而這個專案
      沒有測試用的 PostgreSQL。所以這條釘的是「語意沒有被改掉」，
      實際行為另以正式庫實查佐證（2026-08-29：當天 00:00 那批列的
      fetched_at 是 19:55，證明每次成功都重寫整天）。
    """
    sql = ' '.join(db._UPSERT_CURVE.split())
    assert 'ON CONFLICT (observed_at, kind, label) DO UPDATE' in sql, sql
    assert 'mw = EXCLUDED.mw' in sql, '要更新 mw，否則舊的錯值永遠凍住'
    assert 'DO NOTHING' not in sql


def _fake_bodies():
    fix = Path(__file__).parent / 'fixtures' / '20260829'
    return {n: (fix / n).read_bytes() for n in
            ('loadfueltype.csv', 'loadareas.csv', 'genloadareaperc.csv',
             'unitdata.json')}


def test_broken_fuel_side_does_not_take_the_area_side_down(monkeypatch):
    """★★ 能源別欄位驗證失敗 → 只丟能源別，區域別照常寫入。

    這正是 8/28 那個錯誤的反面。同時檢查**狀態不可以是 ok**——
    只寫了一半卻報 ok，就是「壞掉長得跟正常一樣」的老形狀。
    """
    import run_once

    written = {}

    class FakeDB:
        DatabaseError = RuntimeError
        @staticmethod
        def make_engine(cfg):
            return 'FAKE'
        @staticmethod
        def upsert_points(engine, points, fetched_at):
            written['kinds'] = {p.kind for p in points}
            written['n'] = len(points)
            return len(points), 0
        @staticmethod
        def insert_fetch_run(engine, **kw):
            written['run'] = kw

    class FakeResult:
        bodies = _fake_bodies()
        errors = {}
        http_status = 200

    monkeypatch.setattr(run_once, 'db', FakeDB)
    monkeypatch.setattr(run_once.fetch, 'fetch_all', lambda *a, **k: FakeResult())
    monkeypatch.setattr(run_once.archive, 'archive_run', lambda *a, **k: (None, None))
    monkeypatch.setattr(run_once.archive, 'prune', lambda *a, **k: 0)
    # 讓欄位驗證必定失敗（模擬台電把核能放回第一欄）
    monkeypatch.setattr(run_once.P, 'verify_fuel_columns',
                        lambda *a, **k: ['太陽能：曲線=7016.0、逐機組=0.0（差 +7016.0 MW）'])

    now = datetime(2026, 8, 29, 20, 55, tzinfo=P.TAIPEI)
    status, items, errors = run_once._run({'crawler': {'user_agent': 'x'}}, now, 0.0)

    assert 'fuel' not in written['kinds'], '欄位驗證失敗的那一側必須丟掉'
    assert 'area' in written['kinds'], '★ 另一側不可以陪葬——那就是 8/28 的錯'
    assert 'capacity' not in written['kinds'], \
        '沒有能源別當錨就驗不了 capacity 的時點，一併不寫'
    assert status == 'error', '只寫了一半就不可以報 ok'
    note = written['run']['note']
    assert '欄位驗證失敗' in note and '丟棄能源別' in note, note

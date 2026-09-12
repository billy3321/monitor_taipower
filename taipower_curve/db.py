"""寫入 Cloud SQL。

★ 不建表、不做 migration——表由 dashboard-app 的 alembic_monitor 管理。
  這個專案只 INSERT/UPDATE。
"""
from datetime import datetime
import logging

from psycopg2.extras import execute_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.exc import IntegrityError, ProgrammingError

from . import config as cfgmod
from .parser import Point

log = logging.getLogger(__name__)

SOURCE_ID = 'taipower_loadcurve'          # 已登記在 dashboard-app 的 registry

# ★ DO UPDATE 不是 DO NOTHING：檔案是當日累積、每次抓都大量重疊，
#   而且台電事後會回頭修同一個時間點的值。
#   VALUES %s 給 psycopg2 的 execute_values 用：一天滿檔 2300+ 筆，
#   逐筆 upsert 走 WAN 一筆一個往返要一分多鐘，批次只要幾秒。
_UPSERT_CURVE = """
INSERT INTO monitor_power_load_curve
    (observed_at, kind, label, mw, parser_version, fetched_at)
VALUES %s
ON CONFLICT (observed_at, kind, label) DO UPDATE
   SET mw = EXCLUDED.mw,
       parser_version = EXCLUDED.parser_version,
       fetched_at = EXCLUDED.fetched_at
"""

# ★ append-only：一次執行一列，不覆蓋不更新。
#   CAST() 不能寫成 ::timestamptz——SQLAlchemy 的 text() 不把緊跟著冒號的
#   :param 當參數（:span_lo:: 會原樣送出，資料庫端直接 syntax error）。
_INSERT_RUN = text("""
INSERT INTO monitor_fetch_run
    (source_id, fetched_at, status, record_count, data_timestamp,
     covered_span, http_status, duration_ms, note, raw_uri, raw_sha256)
VALUES (:source_id, :fetched_at, :status, :record_count, :data_timestamp,
        CASE WHEN CAST(:span_lo AS timestamptz) IS NULL THEN NULL
             ELSE tstzrange(CAST(:span_lo AS timestamptz),
                            CAST(:span_hi AS timestamptz), '[]') END,
        :http_status, :duration_ms, :note, :raw_uri, :raw_sha256)
""")


BACKUP_NOTE_PREFIX = '備援'


def backup_marker(instance_id: str) -> str:
    """備援接手時打在 note 最前面的記號。

    ★★ 它同時是**兩件事**：給人看的「這列是備援寫的」，以及給程式看的
       「這列是我自己寫的，不算數」。兩邊必須是同一個字串，所以只有這一個
       定義處——寫成兩份遲早漂移，而漂移之後備援會把自己的列當成主端的，
       然後**永遠不接手**，且看起來完全正常。
    """
    return f'{BACKUP_NOTE_PREFIX}({instance_id})'


# ★ 備援模式唯一要問資料庫的事：「**除了我以外**，最近有沒有人成功寫進來」。
#
# ★★ `NOT LIKE :own_marker` 這一段不是潔癖，是 23:59 那一次的命脈：
#    主端死掉的日子，備援 23:56 接手成功、寫下一列；三分鐘後的 23:59
#    （專門用來收當日最後那個 23:50 點的那一次）如果把自己 23:56 那列
#    也算成「最近有人成功」，就會待命不動——於是**每天固定少掉 23:50
#    前後那幾個點，而且圖上看起來只像那時候沒用電**。
#    這正是 CLAUDE.md 排程那一節花了一整段消滅的形狀。
#    每小時 :56 的節奏下自己上次成功是 60 分鐘前、本來就落在窗外，
#    是 23:59 這個加班場次讓「自己也算數」變成錯的。
_LAST_SUCCESS = text("""
SELECT max(fetched_at) FROM monitor_fetch_run
 WHERE source_id = :source_id
   AND status IN ('ok', 'no_coverage')
   AND (note IS NULL OR note NOT LIKE :own_marker)
""")


class DatabaseError(Exception):
    """★ 與「抓不到台電」是兩種完全不同的失敗，訊息要分清楚，
    否則會叫人去修錯的東西（見 docs/DEPLOY.md）。"""


def make_engine(cfg: dict) -> Engine:
    db = cfgmod.database(cfg)
    url = URL.create(                      # 用 URL.create 才不會被密碼裡的 , : 咬到
        'postgresql+psycopg2',
        username=db['user'], password=db['password'],
        host=db['host'], port=db['port'], database=db['database'])
    connect_args = {'connect_timeout': 15}
    if db.get('ssl_enabled', True):
        connect_args |= {
            'sslmode': db.get('ssl_mode', 'require'),
            'sslrootcert': cfgmod.resolve_path(db['ssl_ca_path']),
            'sslcert': cfgmod.resolve_path(db['ssl_cert_path']),
            'sslkey': cfgmod.resolve_path(db['ssl_key_path']),
        }
    return create_engine(url, connect_args=connect_args,
                         echo=db.get('echo', False), pool_pre_ping=True)


def upsert_points(engine: Engine, points: list[Point],
                  fetched_at: datetime) -> tuple[int, int]:
    """整批 upsert。回 (寫入筆數, 跳過的壞列數)。

    ★ 標準 #6：一列寫不進去不要拖垮整批。先走整批（單一交易，最快），
      整批失敗才退回逐列——每列包 SAVEPOINT，壞列跳過、好列照寫。
      呼叫端看到壞列數 >0 要把該次狀態降為 error 並寫進健康紀錄：
      **救回來不等於沒事**，靜默漏資料比整批失敗更危險。

    兩條路都全滅（例如連線根本建不起來）才丟 DatabaseError。
    """
    if not points:
        return 0, 0
    from .parser import PARSER_VERSION
    rows = [(p.observed_at, p.kind, p.label, p.mw, PARSER_VERSION, fetched_at)
            for p in points]
    try:
        with engine.begin() as conn:       # begin() = 成功才 commit，例外自動 rollback
            execute_values(conn.connection.cursor(), _UPSERT_CURVE, rows,
                           page_size=500)
        return len(rows), 0
    except Exception as exc:
        # raw cursor 丟的是 psycopg2 原生例外，不是 SQLAlchemy 包裝的
        log.warning('整批 upsert 失敗（%s — %s），退回逐列寫入',
                    type(exc).__name__, str(exc)[:150])

    written = failed = 0
    try:
        with engine.begin() as conn:
            cur = conn.connection.cursor()
            for row in rows:
                cur.execute('SAVEPOINT row_sp')
                try:
                    execute_values(cur, _UPSERT_CURVE, [row])
                    cur.execute('RELEASE SAVEPOINT row_sp')
                    written += 1
                except Exception as exc:
                    cur.execute('ROLLBACK TO SAVEPOINT row_sp')
                    failed += 1
                    if failed <= 3:        # 全列印會刷爆日誌，前三筆夠定位
                        log.error('壞列跳過 %r：%s', row[:3], str(exc)[:150])
    except Exception as exc:
        raise DatabaseError(_permission_hint(exc)) from exc
    log.warning('逐列退回結果：寫入 %d、跳過 %d', written, failed)
    return written, failed


def insert_fetch_run(engine: Engine, *, fetched_at: datetime, status: str,
                     record_count: int | None, data_timestamp: datetime | None,
                     span_lo: datetime | None, span_hi: datetime | None,
                     http_status: int | None, duration_ms: int | None,
                     note: str | None, raw_uri: str | None = None,
                     raw_sha256: str | None = None) -> None:
    """★★ 每次執行都要寫一筆，失敗也要寫。

    少了它，這支爬蟲在「資料健康」頁面上等於不存在——而且因為前端有退回機制，
    畫面看起來完全正常，沒人會發現它死了。
    """
    params = {
        'source_id': SOURCE_ID, 'fetched_at': fetched_at, 'status': status,
        'record_count': record_count, 'data_timestamp': data_timestamp,
        'span_lo': span_lo, 'span_hi': span_hi, 'http_status': http_status,
        'duration_ms': duration_ms, 'note': note, 'raw_uri': raw_uri,
        'raw_sha256': raw_sha256,
    }
    try:
        with engine.begin() as conn:
            conn.execute(_INSERT_RUN, params)
    except IntegrityError as exc:
        raise DatabaseError(
            f'寫 monitor_fetch_run 違反外鍵——表示 registry 還沒 sync：'
            f'在 dashboard-app 跑 scripts/monitor/sync_registry.py。原文：{str(exc)[:160]}'
        ) from exc
    except ProgrammingError as exc:
        raise DatabaseError(_permission_hint(exc)) from exc
    except Exception as exc:
        raise DatabaseError(f'寫 monitor_fetch_run 失敗（資料庫端，不是台電端）：'
                            f'{type(exc).__name__} — {str(exc)[:200]}') from exc


def last_success_at(engine: Engine, own_marker: str) -> datetime | None:
    """**別人**最後一次成功寫入這個來源是什麼時候。一次都沒有回 None。

    own_marker 是 backup_marker() 產生的記號，帶這個開頭的列是自己寫的，
    不算數（理由見 _LAST_SUCCESS 上面那段：23:59 那一次會被自己擋住）。

    ★ `no_coverage` 也算成功：它的意思是「抓到了也寫得進，只是來源當下
      還沒有東西」（例如剛過午夜那一次）。主端在那個狀態下是活著的，
      備援接手去抓也一樣拿不到東西。只有 `error` 才代表主端沒做到事。

    ★★ 回 None 是「從來沒成功過」**不是「剛剛成功」**——呼叫端要當成
       「主端不在」而接手。跟 state.unverified_for 回 None 同一個立場：
       未知≠零，也≠沒事。
    """
    try:
        with engine.connect() as conn:
            return conn.execute(
                _LAST_SUCCESS,
                {'source_id': SOURCE_ID, 'own_marker': own_marker + '%'}).scalar()
    except Exception as exc:
        raise DatabaseError(
            f'查不到 monitor_fetch_run 的最後成功時間，備援無從判斷該不該接手'
            f'（資料庫端，不是台電端）：{type(exc).__name__} — {str(exc)[:200]}'
        ) from exc


def _permission_hint(exc: Exception) -> str:
    msg = str(exc)
    if 'permission denied' in msg.lower():
        return ('資料庫拒絕寫入（permission denied）——確認 config.yml 的帳號是 '
                'dashboard 不是 crawler：這張表的寫入權限只授予 dashboard。'
                f'原文：{msg[:160]}')
    return f'SQL 失敗（資料庫端，不是台電端）：{msg[:200]}'

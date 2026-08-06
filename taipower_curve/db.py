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
                  fetched_at: datetime) -> int:
    """整批 upsert，包在單一 transaction——★ 不可半寫入，失敗就整批 rollback。"""
    if not points:
        return 0
    from .parser import PARSER_VERSION
    rows = [(p.observed_at, p.kind, p.label, p.mw, PARSER_VERSION, fetched_at)
            for p in points]
    try:
        with engine.begin() as conn:       # begin() = 成功才 commit，例外自動 rollback
            execute_values(conn.connection.cursor(), _UPSERT_CURVE, rows,
                           page_size=500)
    except Exception as exc:
        # raw cursor 丟的是 psycopg2 原生例外，不是 SQLAlchemy 包裝的
        raise DatabaseError(_permission_hint(exc)) from exc
    return len(rows)


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


def _permission_hint(exc: Exception) -> str:
    msg = str(exc)
    if 'permission denied' in msg.lower():
        return ('資料庫拒絕寫入（permission denied）——確認 config.yml 的帳號是 '
                'dashboard 不是 crawler：這張表的寫入權限只授予 dashboard。'
                f'原文：{msg[:160]}')
    return f'SQL 失敗（資料庫端，不是台電端）：{msg[:200]}'

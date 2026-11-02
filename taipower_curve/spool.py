"""離線暫存：資料庫連不上時把這次要寫的東西先存在本機，下次連得上再回傳。

    data/spool/20261102T035602Z.json     ← 一次執行一個檔
        {"fetched_at": ..., "points": [[observed_at, kind, label, mw], ...],
         "fetch_run": {insert_fetch_run 的參數}}

★★ 為什麼需要它：台電的檔是**當日累積、00:00 歸零、沒有歷史**。資料庫連不上
   的那段時間如果跨過午夜，當天最後那幾個點就永久取不回來——不是「晚一點再抓」
   能補的。原文歸檔（archive.py）雖然留著 bytes，但要重跑整條管線；這裡存的是
   **已經過欄位驗證與各道閘門**的結果，回傳時直接寫，不重新判斷。

★★ 回傳時**先看資料庫裡已經有什麼，有的就不動**（db.fill_missing_points）：
   只寫資料庫沒有的列、或 mw 是 NULL（未報告）而暫存裡有值的列。暫存是**舊的**
   抓取，那段期間主端或之後的執行可能已經寫了台電更正過的新值——舊暫存若用
   一般的 DO UPDATE 回傳，會把更正蓋回舊值，而且畫面看起來完全正常。
   ★ 回傳在這次自己的寫入**之前**、由舊到新：同一天有好幾個暫存時，較舊的
     先把缺口補上，這次新抓的再用一般 upsert 蓋過去，最後留下的一定是最新的。

★ 只回傳資料庫，**不暫存遙測**：gauge 是「當下」的狀態，事後補推一個舊的
  last_success 只會誤導告警。

★ 暫存檔回傳成功才刪。回傳途中又斷線就停下、剩下的留到下次——每次執行都會
  在 log 寫出還有幾個沒回傳，卡住的暫存不會無聲無息。
"""
from datetime import datetime, timezone
from pathlib import Path
import json
import logging

from .parser import TAIPEI, Point

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
SPOOL_DIR = ROOT / 'data' / 'spool'

_DT_FIELDS = ('fetched_at', 'data_timestamp', 'span_lo', 'span_hi')


def save(fetch_run: dict, points: list[Point]) -> Path:
    """存一次執行。fetch_run 是 db.insert_fetch_run 的關鍵字參數。

    ★ datetime 一律存 aware isoformat（帶偏移），讀回來仍是 aware——
      naive 進 timestamptz 會被資料庫拿連線時區去猜（見 CLAUDE.md 時區節）。
    """
    SPOOL_DIR.mkdir(parents=True, exist_ok=True)
    run = {k: (v.isoformat() if isinstance(v, datetime) else v)
           for k, v in fetch_run.items()}
    fetched_at: datetime = fetch_run['fetched_at']
    doc = {
        'fetched_at': fetched_at.isoformat(),
        'points': [[p.observed_at.isoformat(), p.kind, p.label, p.mw]
                   for p in points],
        'fetch_run': run,
    }
    # 檔名用 UTC 時戳：排序即時間順序，不受系統時區影響
    path = SPOOL_DIR / f'{fetched_at.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}.json'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)                      # 先寫暫存再改名：斷電不留半個檔
    log.warning('資料庫連不上，已離線暫存：%s（%d 點）——連得上之後會自動回傳',
                path.name, len(points))
    return path


def pending() -> list[Path]:
    if not SPOOL_DIR.exists():
        return []
    return sorted(SPOOL_DIR.glob('*.json'))   # 檔名即時間，sorted = 由舊到新


def _load(path: Path) -> tuple[dict, list[Point]]:
    doc = json.loads(path.read_text(encoding='utf-8'))
    run = {k: (datetime.fromisoformat(v) if k in _DT_FIELDS and v else v)
           for k, v in doc['fetch_run'].items()}
    points = [Point(datetime.fromisoformat(t), kind, label, mw)
              for t, kind, label, mw in doc['points']]
    return run, points


def flush(db, engine, now: datetime) -> bool:
    """由舊到新回傳暫存。回 False ＝ 途中資料庫連不上（剩下的留著）。

    db 由呼叫端傳入（run_once 的 db 模組），測試才換得掉。
    暫存是空的時候**一次資料庫呼叫都不做**。
    """
    files = pending()
    if not files:
        return True
    log.warning('有 %d 個離線暫存待回傳（最舊 %s）', len(files), files[0].name)
    done = 0
    for path in files:
        try:
            run, points = _load(path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # 壞檔不能卡住後面的暫存，也不能默默刪掉——改名留著給人看
            log.error('離線暫存 %s 讀不回來，改名為 .bad 留存：%s', path.name, exc)
            path.replace(path.with_suffix('.bad'))
            continue
        note = run.get('note') or ''
        try:
            if points:
                written, failed = db.fill_missing_points(
                    engine, points, fetched_at=run['fetched_at'])
                # record_count 記「這次真的補進去幾列」；其餘早已在庫裡，寫在 note
                run['record_count'] = written
                note += f'; 已在庫 {len(points) - written - failed} 點未覆寫'
                if failed:
                    run['status'] = 'error'
                    note += f'; 壞列跳過 {failed} 筆'
            run['note'] = (note + f'; 離線暫存→{now.astimezone(TAIPEI):%m-%d %H:%M}回傳'
                           ).lstrip('; ')[:500]
            db.insert_fetch_run(engine, **run)
        except db.DatabaseError as exc:
            log.error('離線暫存回傳中斷（資料庫仍連不上），剩 %d 個留到下次：%s',
                      len(files) - done, exc)
            return False
        path.unlink()
        done += 1
        log.info('離線暫存已回傳：%s（%d 點中補寫 %d）', path.name, len(points),
                 run.get('record_count') or 0)
    return True

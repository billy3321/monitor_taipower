#!/usr/bin/env python3
"""正式進入點：抓三支 CSV → 解析 → 交叉檢查 → upsert → fetch_run → 遙測。

launchd 每小時跑一次（deployment/tw.nics.taipower-curve.plist）。
短命程序：跑完就結束，不常駐。

失敗分級（★ 兩種失敗長得完全不一樣，訊息要讓人一眼分清楚）：
  - 抓不到台電（403/HTML/逾時）→ 台電端。status='error'，record_count=NULL。
  - 連不上資料庫             → DB 端（IP 沒授權？憑證權限？）。
    fetch_run 也寫不了，至少把遙測推出去。
"""
from datetime import datetime, timezone
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))               # launchd 的 cwd 是專案根，但保險起見

from taipower_curve import config as cfgmod          # noqa: E402
from taipower_curve import db, fetch, telemetry      # noqa: E402
from taipower_curve import parser as P               # noqa: E402

log = logging.getLogger('run_once')


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S')
    started = time.monotonic()
    now = datetime.now(timezone.utc)
    cfg = cfgmod.load()

    errors = 0
    items = 0
    status = 'error'
    note_parts: list[str] = []
    points: list[P.Point] = []
    http_status = None

    # ── 1. 抓 ────────────────────────────────────────────────────
    result = fetch.fetch_all(cfg['crawler']['user_agent'],
                             delay=float(cfg['crawler'].get('fetch_delay', 1.0)))
    http_status = result.http_status
    for name, why in result.errors.items():
        log.error('台電端抓取失敗：%s', why)
        errors += 1
    for name, body in result.bodies.items():
        log.info('抓到 %s（%d bytes）', name, len(body))

    # ── 2. 解析 ──────────────────────────────────────────────────
    b = result.bodies
    try:
        points = P.parse_all(b.get('loadfueltype.csv'), b.get('loadareas.csv'),
                             b.get('genloadareaperc.csv'))
    except P.ParseError as exc:
        # 欄數不符＝來源改版。寧可整次失敗也不要猜著對寫進錯的標籤。
        log.error('解析失敗（來源可能改版，欄位對應要人工重新驗證）：%s', exc)
        errors += 1
        points = []

    # ── 3. 交叉檢查：兩支曲線總和必須吻合 ─────────────────────────
    kinds = {p.kind for p in points}
    if {'fuel', 'area'} <= kinds:
        checked = P.cross_check(points)
        if checked is None:
            log.error('兩支曲線沒有共同時間點——時間欄格式可能變了，這次不寫入')
            errors += 1
            points = []
        else:
            t, ftot, atot = checked
            diff = abs(ftot - atot)
            if diff >= P.CROSS_CHECK_TOLERANCE_MW:
                # ★ 總和分岔＝欄序錯置的訊號。寫進去的話整張圖標籤錯位，
                #   而且畫面看起來完全正常——這是本專案最怕的靜默失敗。
                log.error('交叉檢查失敗：%s 能源別 %.0f MW vs 區域別 %.0f MW（差 %.0f）'
                          '——疑似欄序錯置，這次不寫入', t, ftot, atot, diff)
                errors += 1
                points = []
            else:
                log.info('交叉檢查通過：%s 兩邊總和差 %.0f MW', t, diff)

    # ── 4. 寫入 ──────────────────────────────────────────────────
    curve_times = sorted({p.observed_at for p in points})
    try:
        engine = db.make_engine(cfg)
        items = db.upsert_points(engine, points, fetched_at=now)
        if items:
            log.info('已 upsert %d 筆（%s）', items,
                     '; '.join(f'{k}={sum(1 for p in points if p.kind == k)}'
                               for k in ('fuel', 'area', 'area_gen', 'area_load')
                               if k in kinds))

        if errors == 0 and items > 0:
            status = 'ok'
        elif errors == 0 and items == 0:
            status = 'no_coverage'          # 抓到了但整天還沒有任何有值時點
        else:
            status = 'error'

        for name in fetch.FILES:
            if name in result.errors:
                note_parts.append(f'{name} 失敗')
        counts = {k: sum(1 for p in points if p.kind == k) for k in sorted(kinds)}
        note_parts.append('; '.join(f'{k}={v}' for k, v in counts.items()) or '無資料')

        # ★★ 每次執行都要寫一筆 fetch_run，失敗也要寫——
        #    少了它，這支爬蟲在「資料健康」頁面上等於不存在。
        db.insert_fetch_run(
            engine, fetched_at=now, status=status,
            record_count=items if points or status != 'error' else None,
            data_timestamp=curve_times[-1] if curve_times else None,
            span_lo=curve_times[0] if curve_times else None,
            span_hi=curve_times[-1] if curve_times else None,
            http_status=http_status,
            duration_ms=int((time.monotonic() - started) * 1000),
            note='; '.join(note_parts)[:500],
            raw_uri=fetch.BASE)
        log.info('fetch_run 已記錄：status=%s record_count=%s',
                 status, items if points or status != 'error' else None)
    except db.DatabaseError as exc:
        log.error('%s', exc)
        errors += 1
        status = 'error'

    # ── 5. 遙測（最後防線：連 DB 都失敗時它是唯一的求救訊號）────────
    telemetry.push(cfg, run_ts=now.timestamp(), success=(status == 'ok'),
                   items=items, errors=errors,
                   duration=time.monotonic() - started)

    log.info('結束：status=%s items=%d errors=%d %.1fs',
             status, items, errors, time.monotonic() - started)
    return 0 if status == 'ok' else 1


if __name__ == '__main__':
    sys.exit(main())

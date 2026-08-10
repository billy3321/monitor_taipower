#!/usr/bin/env python3
"""正式進入點：抓 → 歸檔原文 → 解析 → 交叉檢查 → upsert → fetch_run → 遙測。

launchd 每小時跑一次（deployment/tw.nics.taipower-curve.plist）。
短命程序：跑完就結束，不常駐。

失敗分級（★ 幾種失敗長得完全不一樣，訊息要讓人一眼分清楚）：
  - 抓不到台電（403/HTML/逾時）→ 台電端。status='error'，record_count=NULL。
  - 連不上資料庫             → DB 端（IP 沒授權？憑證權限？）。
    fetch_run 也寫不了，至少把遙測推出去。
  - 程式自己爆掉             → 最外層還是會推遙測（errors>0），
    否則「爬蟲壞了」會長得跟「機器關機了」一樣，兩者要修的東西完全不同。
"""
from datetime import datetime, timezone
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))               # launchd 的 cwd 是專案根，但保險起見

from taipower_curve import archive                   # noqa: E402
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
    cfg = None
    errors = 0
    items = 0
    status = 'error'

    try:
        cfg = cfgmod.load()
        status, items, errors = _run(cfg, now, started)
    except Exception as exc:                          # noqa: BLE001
        # ★ 最外層防線：任何沒預期到的例外都不能讓這支「安靜地死掉」。
        log.exception('未預期的例外，這次執行失敗：%s', exc)
        errors += 1
        status = 'error'

    # ★ 遙測一定要推——失敗也要推。沒有遙測 = 爬蟲死了沒人知道。
    if cfg is not None:
        telemetry.push(cfg, run_ts=now.timestamp(), success=(status == 'ok'),
                       items=items, errors=errors,
                       duration=time.monotonic() - started)
    else:
        log.error('連 config 都讀不到，無法推遙測——存活告警會因為久未更新而燒，'
                  '那是正確行為')

    log.info('結束：status=%s items=%d errors=%d %.1fs',
             status, items, errors, time.monotonic() - started)
    return 0 if status == 'ok' else 1


def _run(cfg: dict, now: datetime, started: float) -> tuple[str, int, int]:
    errors = 0
    items = 0
    note_parts: list[str] = []
    points: list[P.Point] = []

    # ── 1. 抓 ────────────────────────────────────────────────────
    result = fetch.fetch_all(cfg['crawler']['user_agent'],
                             delay=float(cfg['crawler'].get('fetch_delay', 1.0)))
    http_status = result.http_status
    for name, why in result.errors.items():
        log.error('台電端抓取失敗：%s', why)
        errors += 1
        note_parts.append(f'{name} 失敗')
    for name, body in result.bodies.items():
        log.info('抓到 %s（%d bytes）', name, len(body))

    # ── 2. 歸檔原文（★ 在解析之前——解析失敗才是最需要原文的時候）──
    raw_uri = raw_sha = None
    try:
        run_dir, raw_sha = archive.archive_run(result.bodies, fetch.source_url,
                                               fetched_at=now)
        if run_dir is not None:
            raw_uri = str(run_dir)
            log.info('原文已歸檔：%s（%d 個檔）', run_dir, len(result.bodies))
        archive.prune()
    except Exception as exc:                          # noqa: BLE001
        # 歸檔失敗不該讓整次執行失敗——資料進得了資料庫比留副本重要
        log.warning('原文歸檔失敗（不影響寫入）：%s — %s', type(exc).__name__, exc)
        errors += 1

    # ── 3. 解析（逐檔隔離：單檔解析失敗 ≈ 該檔沒抓到，其他照常）────
    points, parse_errors = P.parse_files(result.bodies)
    for why in parse_errors:
        # 欄數不符＝來源改版。該檔寧可失敗也不要猜著對寫進錯的標籤。
        log.error('解析失敗（來源可能改版，欄位對應要人工重新驗證）：%s', why)
        errors += 1
        note_parts.append(why.split('：')[0])

    # ── 3.5 跨午夜防線：未來的點一律整批拒寫 ─────────────────────
    #   慢速抓取跨過 00:00 時檔案可能已換日重置，舊日資料會被 perc 的
    #   新日期標成「未來」。這批寫進去會變成掛在圖上的整天假資料。
    future = P.find_future_points(points, now)
    if future:
        log.error('出現 %d 個未來時點（最遠 %s）——疑似跨午夜抓到換日中的檔案，'
                  '這次整批不寫入', len(future),
                  max(p.observed_at for p in future))
        errors += 1
        points = []

    # ── 4. 交叉檢查 ──────────────────────────────────────────────
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

    # loadpara 的即時用電 vs 能源別合計（驗時點掛對了沒有）。
    # loadpara 偶爾比曲線慢一格，rehome 會往回找吻合的時點改掛——
    # 慢一格不算錯；連往回找都找不到才是真的不同步。
    if any(p.kind == 'capacity' for p in points):
        orig_anchor = next(p.observed_at for p in points if p.kind == 'capacity')
        rehomed = P.rehome_capacity(points)
        if rehomed is None:
            log.error('即時用電對不上最近幾個時點的能源別合計——loadpara 與曲線'
                      '真的不同步（不只是慢一格），這次不寫 capacity')
            errors += 1
            points = [p for p in points if p.kind != 'capacity']
        else:
            points, anchored_at, diff = rehomed
            if anchored_at != orig_anchor:
                log.info('即時供電檢查：loadpara 慢曲線一格，capacity 改掛 %s'
                         '（該時點差 %.0f MW）', anchored_at, diff)
            else:
                log.info('即時供電檢查通過：即時用電與能源別合計差 %.0f MW', diff)

    # ── 5. 寫入 ──────────────────────────────────────────────────
    kinds = {p.kind for p in points}
    curve_times = sorted({p.observed_at for p in points})
    try:
        engine = db.make_engine(cfg)
        items = db.upsert_points(engine, points, fetched_at=now)
        if items:
            counts = {k: sum(1 for p in points if p.kind == k) for k in sorted(kinds)}
            log.info('已 upsert %d 筆（%s）', items,
                     '; '.join(f'{k}={v}' for k, v in counts.items()))
            note_parts.append('; '.join(f'{k}={v}' for k, v in counts.items()))
        else:
            note_parts.append('無資料')

        if errors == 0 and items > 0:
            status = 'ok'
        elif errors == 0 and items == 0:
            status = 'no_coverage'          # 抓到了但整天還沒有任何有值時點
        else:
            status = 'error'

        # ★★ 每次執行都要寫一筆 fetch_run，失敗也要寫。
        db.insert_fetch_run(
            engine, fetched_at=now, status=status,
            record_count=items if points or status != 'error' else None,
            data_timestamp=curve_times[-1] if curve_times else None,
            span_lo=curve_times[0] if curve_times else None,
            span_hi=curve_times[-1] if curve_times else None,
            http_status=http_status,
            duration_ms=int((time.monotonic() - started) * 1000),
            note='; '.join(note_parts)[:500],
            raw_uri=raw_uri, raw_sha256=raw_sha)
        log.info('fetch_run 已記錄：status=%s record_count=%s', status, items)
    except db.DatabaseError as exc:
        # ★ 與「抓不到台電」是兩種完全不同的失敗，訊息已經在 DatabaseError 裡分好
        log.error('%s', exc)
        errors += 1
        status = 'error'
    return status, items, errors


if __name__ == '__main__':
    sys.exit(main())

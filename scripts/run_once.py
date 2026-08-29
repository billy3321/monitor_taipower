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
from taipower_curve import state                     # noqa: E402
from taipower_curve import config as cfgmod           # noqa: E402
from taipower_curve import db, fetch, telemetry       # noqa: E402
from taipower_curve import parser as P                # noqa: E402

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
        telemetry.push(cfg, run_ts=now.timestamp(), status=status,
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

    # ── 3.2 欄位對應驗證（按名字，不是按順序）★★ ──────────────────
    #   曲線 CSV 沒有標頭列，12 欄／4 欄的意義只寫在圖表的 JavaScript 裡。
    #   在此之前我們把欄序寫死在 parser.py，等於**假設台電不會改**——而
    #   load_fueltype_.html 裡第一欄的「核能」只是被 /* */ 註解掉，
    #   核能一旦重啟、註解拿掉，12 欄全部位移一格，我們會把每一種發電方式
    #   都標錯，**而圖表看起來完全正常，可能好幾個月沒人發現**。
    #
    #   開放資料 d006001（unitdata.json）是這批來源裡唯一自己帶欄位名稱的，
    #   所以每次跑都拿它按「機組類型」逐類對帳。對不上就不寫，並指名是哪一類。
    verified = False
    if 'unitdata.json' in result.bodies:
        try:
            stamp, unit_totals = P.parse_unitdata(result.bodies['unitdata.json'])
            problems = P.verify_fuel_columns(points, stamp, unit_totals)
            if problems:
                # ★★ 欄位意義變了，能源別寫進去的每一筆都會是錯的標籤，
                #    而且畫面正常——必須丟掉。
                # ★ 但**只丟能源別那一側**：區域別是另一支檔、另一組欄位，
                #   跟這次的問題無關。整批丟掉就是 2026-08-28 那個錯誤的
                #   形狀（台電一邊出問題，我們把整天的資料都不要了）。
                dropped = sum(1 for p in points if p.kind == 'fuel')
                log.error('欄位對應驗證失敗（來源可能改版，欄位對應要人工重新'
                          '確認後改 FUEL_COLUMNS 與 PARSER_VERSION）：%s'
                          '——丟棄能源別 %d 點，區域別照常寫入',
                          '；'.join(problems), dropped)
                errors += 1
                note_parts.append(f'欄位驗證失敗：{problems[0]}')
                note_parts.append(f'丟棄能源別 {dropped} 點')
                points = [p for p in points if p.kind != 'fuel']
            else:
                verified = True
                state.mark_verified(now)
                log.info('欄位對應驗證通過：%s 逐機組 12 類逐類吻合', stamp)
        except P.ParseError as exc:
            # 驗證資料本身壞掉 ≈ 沒抓到，不該讓曲線一起陪葬（與其他檔同一原則）
            log.warning('unitdata.json 解析失敗，本次欄位對應未驗證：%s', exc)
            note_parts.append('欄位未驗證(解析失敗)')
    else:
        note_parts.append('欄位未驗證(抓取失敗)')
    if not verified and points:
        # ★★ 退回「按寫死的欄序解析」是**降級模式**，不是正常狀態。單次退回
        #    只記 note（驗證資料壞掉不該讓曲線陪葬），但**不能無聲無息變成
        #    常態**：台電哪天把那支開放資料的網址換掉，我們會一路退回猜測
        #    模式，而 note 沒有人在看。超過 24 小時沒驗成功就升級成失敗，
        #    讓存活告警燒起來。
        log.warning('本次未經欄位驗證，沿用寫死的欄序——這是降級模式')
        age = state.unverified_for(now)
        if age is None or age > state.MAX_UNVERIFIED:
            how_long = '從來沒驗證成功過' if age is None else f'已經 {age} 沒驗證成功'
            log.error('欄位對應%s（上限 %s）——降級模式不可以變成常態，'
                      '本次標記為失敗讓告警燒起來', how_long, state.MAX_UNVERIFIED)
            errors += 1
            note_parts.append(f'欄位驗證過期({how_long})')

    # ── 3.5 跨午夜防線：未來的點一律整批拒寫 ─────────────────────
    #   慢速抓取跨過 00:00 時檔案可能已換日重置，舊日資料會被 perc 的
    #   新日期標成「未來」。這批寫進去會變成掛在圖上的整天假資料。
    future = P.find_future_points(points, now)
    if future:
        log.error('出現 %d 個未來時點（最遠 %s）——疑似跨午夜抓到換日中的檔案，'
                  '這次整批不寫入', len(future),
                  max(p.observed_at for p in future))
        errors += 1
        # ★ note 要寫得出「是哪一道擋的」。2026-08-29 查這件事時，四道閘門
        #   失敗全都只寫「無資料」三個字，花了十幾次查詢才反推出是交叉檢查。
        note_parts.append(f'未來時點 {len(future)} 個')
        points = []

    # ── 4. 交叉檢查：兩支曲線總和 ────────────────────────────────
    #
    # ★★ 2026-08-29 這條**從「擋下來」降級成「記下來」**，理由是它原本
    #    兼任的工作已經被 3.2 的欄位驗證取代，而它做那份工作做得不好：
    #
    #    它比的是兩個**總和**，所以兩個能源別欄位對調它根本驗不出來
    #    （總和不變）；反過來，台電自己兩份數字對不齊時它會整批擋下。
    #    2026-08-28 08:00 起就是後者：能源別合計比區域別低 0~336 MW
    #    （拿第三個檔 loadpara 的即時用電當裁判，區域別差 ±2、能源別差
    #    −11~−46，是台電的能源別那側在漂），12 欄逐類對帳全部吻合。
    #    結果是**台電自己算不齊，我們把整天的資料丟掉**——8/28 08:00 之後
    #    16 小時因此永久遺失（來源當日歸零、沒有歷史檔）。
    #
    # ★ 所以現在：照樣寫入，把分岔**與「是哪一側在漂」**一起記進 note。
    #   拿 loadpara 的即時用電當獨立裁判（實測 08-19~28 兩側都在 2 MW 內、
    #   08-29 只有能源別跑到 46 MW），所以講得出是哪一邊，不是只說「對不上」。
    #
    # ★★ **刻意沒有上限門檻**（「分岔超過 N 就不寫」）。理由：能源別已經由
    #    3.2 對逐機組 API 逐類驗過、區域別由 loadpara 驗過，兩側各自都是
    #    台電自己說的數字。它們彼此不平衡是**台電的狀態**，不管差多少都是。
    #    設一個上限只會在某天重演 8/28——把好好的資料丟掉。
    #    真的大到不合理時，note 會寫著、人去查台電，而不是機器自己決定不要。
    #
    # ★★ 也**刻意不「湊平」**：短少的 0~336 MW 無法歸屬到任何一種發電方式
    #    （12 類逐類都跟逐機組吻合），補一個「未分類」欄位就是**發明資料**。
    #    正確的用法是：**總量看區域別／即時用電，組成看能源別**（見 CLAUDE.md）。
    #
    # ★ 而寫進去的東西**會自己修正**：upsert 是 ON CONFLICT DO UPDATE，
    #   而且每次跑都重寫整天。台電哪天更正了，我們下一次跑就跟著更正。
    #   「先寫進去、之後會修正」之所以安全，靠的就是這一條。
    kinds = {p.kind for p in points}
    if {'fuel', 'area'} <= kinds:
        checked = P.cross_check(points)
        if checked is None:
            log.error('兩支曲線沒有共同時間點——時間欄格式可能變了，這次不寫入')
            errors += 1
            note_parts.append('兩支曲線無共同時點')
            points = []
        else:
            t, ftot, atot = checked
            diff = ftot - atot
            worst = P.worst_divergence(points)
            note_parts.append(f'曲線分岔 最新{diff:+.0f}/當日最大{worst:+.0f} MW')
            if abs(diff) >= P.CROSS_CHECK_TOLERANCE_MW:
                verdict = P.name_the_drifting_side(P.diagnose_sides(points))
                note_parts.append(verdict)
                log.warning('兩支曲線分岔：%s 能源別 %.0f vs 區域別 %.0f（差 %+.0f）'
                            '——%s。欄位對應已另行驗證，照常寫入（台電更正後'
                            '下次跑會自動蓋回正確值）', t, ftot, atot, diff, verdict)
            else:
                log.info('交叉檢查通過：%s 兩邊總和差 %+.0f MW', t, diff)

    # loadpara 的即時用電 vs 能源別合計（驗時點掛對了沒有）。
    # loadpara 偶爾比曲線慢一格，rehome 會往回找吻合的時點改掛——
    # 慢一格不算錯；連往回找都找不到才是真的不同步。
    # ★★ 能源別被丟掉時，capacity 也要一起丟。rehome 是拿能源別當錨的，
    #    沒有錨就驗不了時點——而這個模組的原則是「驗不了時點的 capacity
    #    寧可不寫，不要掛在猜的時間上」（parse_loadpara 的既有立場）。
    #    照原路走還會印出「不同步」這種誤導訊息：那不是不同步，是沒得比。
    if any(p.kind == 'capacity' for p in points) and not any(
            p.kind == 'fuel' for p in points):
        n_cap = sum(1 for p in points if p.kind == 'capacity')
        log.warning('能源別已被丟棄，capacity 沒有錨可以驗時點——一併不寫（%d 點）',
                    n_cap)
        note_parts.append(f'連帶不寫 capacity {n_cap} 點')
        points = [p for p in points if p.kind != 'capacity']

    if any(p.kind == 'capacity' for p in points):
        orig_anchor = next(p.observed_at for p in points if p.kind == 'capacity')
        rehomed = P.rehome_capacity(points)
        if rehomed is None:
            log.error('即時用電對不上最近幾個時點的能源別合計——loadpara 與曲線'
                      '真的不同步（不只是慢一格），這次不寫 capacity')
            errors += 1
            note_parts.append('即時用電與曲線不同步')
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
    engine = db.make_engine(cfg)
    items = 0
    failed_rows = 0
    write_failed = False
    try:
        items, failed_rows = db.upsert_points(engine, points, fetched_at=now)
    except db.DatabaseError as exc:
        # ★ 與「抓不到台電」是兩種完全不同的失敗，訊息已經在 DatabaseError 裡分好
        log.error('%s', exc)
        errors += 1
        write_failed = True
        note_parts.append('曲線寫入失敗')
    if failed_rows:
        # ★ 救回來不等於沒事：壞列數要看得見，該次狀態降為 error（標準 #6）
        errors += 1
        note_parts.append(f'壞列跳過 {failed_rows} 筆')
    if items:
        counts = {k: sum(1 for p in points if p.kind == k) for k in sorted(kinds)}
        log.info('已 upsert %d 筆（%s）', items,
                 '; '.join(f'{k}={v}' for k, v in counts.items()))
        note_parts.append('; '.join(f'{k}={v}' for k, v in counts.items()))
    elif not points:
        note_parts.append('無資料')

    if errors == 0 and items > 0:
        status = 'ok'
    elif errors == 0 and items == 0:
        status = 'no_coverage'              # 抓到了但整天還沒有任何有值時點
    else:
        status = 'error'

    # record_count 的語意（寫錯會讓監控說謊）：寫入路徑有完成就是實際筆數
    # （含 no_coverage 的 0 與逐列退回的部分筆數）；抓/解析失敗到沒東西可寫、
    # 或寫入本身失敗 → NULL（筆數未知，不是 0）。
    if write_failed or (status == 'error' and not points):
        record_count = None
    else:
        record_count = items

    # ★★ 每次執行都要寫一筆 fetch_run，失敗也要寫——而且**與資料不同交易**
    #    （標準 #5，msil 教訓）：資料那筆交易 rollback 時，「我失敗了」這筆
    #    紀錄不能陪葬，否則壞掉的來源在健康頁上長得跟正常的一模一樣。
    try:
        db.insert_fetch_run(
            engine, fetched_at=now, status=status,
            record_count=record_count,
            data_timestamp=curve_times[-1] if curve_times else None,
            span_lo=curve_times[0] if curve_times else None,
            span_hi=curve_times[-1] if curve_times else None,
            http_status=http_status,
            duration_ms=int((time.monotonic() - started) * 1000),
            note='; '.join(note_parts)[:500],
            raw_uri=raw_uri, raw_sha256=raw_sha)
        log.info('fetch_run 已記錄：status=%s record_count=%s', status, record_count)
    except db.DatabaseError as exc:
        log.error('fetch_run 寫不進去（健康頁會看不到這次執行）：%s', exc)
        errors += 1
        status = 'error'
    return status, items, errors


if __name__ == '__main__':
    sys.exit(main())

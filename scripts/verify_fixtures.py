#!/usr/bin/env python3
"""欄位對應的驗收檢查——**實作完成前就能跑**，不需要任何本專案的程式碼。

用途：CLAUDE.md 寫的欄序是逆向出來的，這支用「兩支曲線總和必須吻合」
把它證明給你看，並印出參考數值。實作完 parser 之後，把同一套斷言
寫進 tests/ 就是驗收條件。

    python3 scripts/verify_fixtures.py              # 用 tests/fixtures 的檔
    python3 scripts/verify_fixtures.py --live       # 改抓即時資料

★ 為什麼「總和吻合」是關鍵：能源別與區域別是同一份用電的兩種切分。
  標錯或漏掉任何一欄，兩邊總和就會分岔。**單看某一欄「看起來合理」
  驗不出欄序錯置**——例如把太陽能與重油對調，白天看起來一樣有起伏。
"""
import argparse
import csv
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / 'tests' / 'fixtures'

FUEL_COLUMNS = [
    '燃氣', '民營電廠-燃氣', '燃煤', '民營電廠-燃煤', '汽電共生', '重油',
    '太陽能', '風力', '水力', '儲能', '其它再生能源', '儲能負載',
]
AREA_COLUMNS = ['東部', '南部', '中部', '北部']
# genloadareaperc.csv：時戳 + 4 區 ×（發電, 用電）
PERC_COLUMNS = ['北部發電', '北部用電', '中部發電', '中部用電',
                '南部發電', '南部用電', '東部發電', '東部用電']

WAN_KW_TO_MW = 10.0


def read(name: str, live: bool) -> bytes:
    if live:
        sys.path.insert(0, str(ROOT / 'scripts'))
        from preflight import session  # noqa
        url = ('https://www.taipower.com.tw/d006/loadGraph/loadGraph/data/' + name)
        return session().get(url, timeout=25).content
    return (FIXTURES / name).read_bytes()


def rows_with_values(body: bytes, n_cols: int):
    """回 [(時間字串, [值…])]。

    ★ 只回**有值**的列：檔案永遠 144 列，未來時段預留成 ','（連時間欄都空）。
      那些不是「未報告」，是還沒發生——整列跳過，不可寫成 0 也不必寫 NULL。
    """
    out = []
    for raw in csv.reader(io.StringIO(body.decode('utf-8-sig'))):
        if len(raw) != n_cols + 1:
            continue                        # 殘缺列（含檔尾的 ','）
        if not raw[0].strip() or not raw[1].strip():
            continue                        # 尚未發生的時段
        out.append((raw[0].strip(), raw[1:]))
    return out


def num(s):
    s = (s or '').strip()
    if s in ('', '-', '—', 'N/A'):
        return None                          # 未知≠零
    try:
        return float(s)
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--live', action='store_true', help='抓即時資料而非 fixture')
    args = ap.parse_args()

    fuel = rows_with_values(read('loadfueltype.csv', args.live), len(FUEL_COLUMNS))
    area = rows_with_values(read('loadareas.csv', args.live), len(AREA_COLUMNS))
    perc = rows_with_values(read('genloadareaperc.csv', args.live), len(PERC_COLUMNS))

    print(f'  能源別 {len(fuel)} 個有值時點、區域別 {len(area)} 個、占比檔 {len(perc)} 列\n')
    if not fuel or not area:
        print('  ✕ 沒有可用資料'); return 1

    # 兩邊各取最後一個共同時間點
    common = {t for t, _ in fuel} & {t for t, _ in area}
    if not common:
        print('  ✕ 兩支檔沒有共同時間點——時間欄格式可能不一致'); return 1
    t = sorted(common)[-1]
    fv = dict(zip(FUEL_COLUMNS, next(v for x, v in fuel if x == t)))
    av = dict(zip(AREA_COLUMNS, next(v for x, v in area if x == t)))

    print(f'  === {t} 能源別 ===')
    ftot = 0.0
    for k in FUEL_COLUMNS:
        v = num(fv[k])
        print(f'    {k:14} {"未報告" if v is None else f"{v * WAN_KW_TO_MW:>10.0f} MW"}')
        if v is not None:
            ftot += v * WAN_KW_TO_MW        # ★ 含負值的儲能負載，與台電同算法
    print(f'    {"合計":14} {ftot:>10.0f} MW')

    print(f'\n  === {t} 區域別 ===')
    atot = 0.0
    for k in AREA_COLUMNS:
        v = num(av[k])
        print(f'    {k:14} {"未報告" if v is None else f"{v * WAN_KW_TO_MW:>10.0f} MW"}')
        if v is not None:
            atot += v * WAN_KW_TO_MW
    print(f'    {"合計":14} {atot:>10.0f} MW')

    diff = abs(ftot - atot)
    print(f'\n  === 交叉驗收：兩支曲線總和差 {diff:.0f} MW ===')
    ok = diff < 50          # 四捨五入誤差內（實測差 1 MW）
    print('  ✓ 欄位對應正確' if ok else
          '  ✕ 總和分岔——欄序或欄數對錯了，回頭看 CLAUDE.md 的對應表')

    if perc:
        pv = dict(zip(PERC_COLUMNS, perc[-1][1]))
        print(f'\n  === {perc[-1][0]} 各區發電/用電（差額＝區域間潮流）===')
        for z in ('北部', '中部', '南部', '東部'):
            g, l = num(pv[f'{z}發電']), num(pv[f'{z}用電'])
            if g is None or l is None:
                continue
            d = (g - l) * WAN_KW_TO_MW
            print(f'    {z}  發電 {g * WAN_KW_TO_MW:>8.0f}  用電 {l * WAN_KW_TO_MW:>8.0f}  '
                  f'{"送出" if d > 0 else "接受"} {abs(d):>7.0f} MW')

    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())

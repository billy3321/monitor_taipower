"""來源檔的解析——純函式，不碰網路與資料庫。

欄序來源見 CLAUDE.md（2026-08-05 從官網現行 JS 分支的 balloon 文字逆向、
並用兩支曲線總和交叉驗證過）。改動這裡的欄位對應時 PARSER_VERSION 要跟著改。
"""
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo
import csv
import io
import json

# 改欄位對應時要跟著改（會寫進 monitor_power_load_curve.parser_version）
PARSER_VERSION = '2026-08-06.1'

# ★ 台電時戳是台北時間且不帶時區，組 observed_at 時必須補上時區。
#
# ★★ 用 IANA 時區 ZoneInfo('Asia/Taipei')，**不要**寫成
#    timezone(timedelta(hours=8))。兩者對今天的資料算出來一樣，但意義不同：
#    前者說的是「台北這個地方的時間」，後者說的是「某個剛好是 +8 的偏移」。
#    寫死偏移的東西一旦遇到時區規則變動（台灣 1979 年以前實施過日光節約時間）
#    就會靜靜地錯，而且錯的是歷史資料重跑——那正是原文歸檔存在的目的。
#    讓 tz 資料庫去回答偏移是多少，不要自己硬編。
TAIPEI = ZoneInfo('Asia/Taipei')

# ★ 來源單位是萬瓩，1 萬瓩 = 10 MW。寫進資料庫前一律換算成 MW，
#   跟 monitor_power_unit_observation 同單位。
WAN_KW_TO_MW = 10.0

# ★ 順序即台電圖上由下往上的堆疊順序，dashboard-app 照這個順序畫。不要自己重排。
FUEL_COLUMNS = [
    '燃氣', '民營電廠-燃氣', '燃煤', '民營電廠-燃煤', '汽電共生', '重油',
    '太陽能', '風力', '水力', '儲能', '其它再生能源', '儲能負載',
]
# ★ 不是「東北中南」。最後一欄對應北部——用官網當日數字對帳確認過。
AREA_COLUMNS = ['東部', '南部', '中部', '北部']

# loadpara.json：kind='capacity'。★ 只取**單位是萬瓩**的欄位。
#
# 百分比欄（curr_util_rate、fore_peak_resv_rate）與文字欄（indicator、
# publish_time、hour_range）**刻意不進資料庫**：mw 欄位的語意就是 MW，
# 把百分比塞進去遲早有人拿它去加總。這些欄位全部保存在原文歸檔裡，
# 而且比率本來就能從這裡的 MW 值回推（使用率＝即時用電/即時供電能力）。
#
# yday_* 也不取：那是昨天的摘要，我們自己的曲線history 已經有昨天全天資料。
LOADPARA_FIELDS = [
    ('curr_load', '即時用電'),
    # ★★ 這個才是台電網頁「使用率」的分母（即時供電能力），
    #    不是 fore_maxi_sply_capacity（那是今日預估最大供電能力）。
    ('real_hr_maxi_sply_capacity', '即時供電能力'),
    ('fore_maxi_sply_capacity', '今日最大供電能力'),
    ('fore_peak_dema_load', '尖峰預估用電'),
    ('fore_peak_resv_capacity', '尖峰預估備轉容量'),
]

# genloadareaperc.csv：完整時戳 + 4 區 ×（發電, 用電）
PERC_COLUMNS = [
    ('area_gen', '北部'), ('area_load', '北部'),
    ('area_gen', '中部'), ('area_load', '中部'),
    ('area_gen', '南部'), ('area_load', '南部'),
    ('area_gen', '東部'), ('area_load', '東部'),
]

# 未知≠零：這些字面意思是「沒有值」，一律 None。'0.0' 才是真的零。
NULL_TOKENS = {'', '-', '—', 'N/A', 'n/a', 'NA', 'null', 'NULL'}

# 兩支曲線是同一份用電的兩種切分，同一時點總和必須吻合（實測差 1 MW）。
CROSS_CHECK_TOLERANCE_MW = 50.0

# loadpara 的即時用電 vs 同時點能源別合計（實測差 0 MW）。放寬到 100 MW 是為了
# 容忍「loadpara 已更新到下一個 10 分鐘、曲線還沒」的短暫不同步。
CAPACITY_CHECK_TOLERANCE_MW = 100.0


class ParseError(Exception):
    """解析失敗。★ 寧可丟例外也不要猜著對——猜錯會讓整張圖標籤錯位，
    而且畫面看起來完全正常，可能好幾天沒人發現。"""


@dataclass(frozen=True)
class Point:
    observed_at: datetime   # aware，+08:00
    kind: str               # fuel / area / area_gen / area_load
    label: str
    mw: float | None        # NULL=未報告，0=真的零出力


def parse_number(raw: str | None) -> float | None:
    """數值欄 → MW。未知回 None，不是 0。

    ★ 認不得的字面丟例外，不要默默當成 None：那是來源改版的訊號，
      吞掉它就會變成「有一整欄長期是 NULL」而沒人知道為什麼。
    """
    s = (raw or '').strip()
    if s in NULL_TOKENS:
        return None
    try:
        value = float(s)
    except ValueError:
        raise ParseError(f'數值欄認不得的字面：{s!r}') from None
    # 來源是 1 位小數的萬瓩，×10 後理論上是整數 MW；round 只為消掉浮點雜訊
    return round(value * WAN_KW_TO_MW, 3)


def parse_time(raw: str, base_date: date) -> datetime:
    """時間欄 → aware datetime。

    ★ 同一個檔裡有兩種寫法：'00:10' 與整點的 '00'（loadareas.csv 首列實測是 '00'）。
    """
    s = raw.strip()
    try:
        if ':' in s:
            hh, mm = s.split(':', 1)
        else:
            hh, mm = s, '0'
        hour, minute = int(hh), int(mm)
    except ValueError:
        raise ParseError(f'時間欄認不得的格式：{raw!r}') from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ParseError(f'時間欄超出範圍：{raw!r}')
    return datetime(base_date.year, base_date.month, base_date.day,
                    hour, minute, tzinfo=TAIPEI)


def _rows(body: bytes):
    return csv.reader(io.StringIO(body.decode('utf-8-sig')))


def parse_curve(body: bytes, kind: str, columns: list[str],
                base_date: date) -> list[Point]:
    """loadfueltype.csv / loadareas.csv → Point。

    每列三分（★ 這三條分開處理是本專案最容易寫錯的地方）：
      1. 整列皆空（未來時段的 ','）→ 跳過。那些時間點還沒到，連「未報告」都算不上，
         **絕不可寫成 0**，也不必寫 NULL。
      2. 時間有值且欄數符合 → 解析；個別數值空 → None（未報告）。
      3. 時間有值但欄數不符 → 丟例外。欄數變動代表來源改版，猜著對的代價太大。
    """
    expected = len(columns) + 1
    points: list[Point] = []
    for lineno, row in enumerate(_rows(body), start=1):
        if all(not cell.strip() for cell in row):
            continue                                    # 1. 尚未發生的時段
        if len(row) != expected:                        # 3. 欄數不符
            raise ParseError(
                f'{kind} 第 {lineno} 列欄數 {len(row)}，預期 {expected}'
                f'（來源可能改版了）：{row!r}')
        if not row[0].strip():
            raise ParseError(f'{kind} 第 {lineno} 列有數值但沒有時間：{row!r}')
        observed_at = parse_time(row[0], base_date)     # 2. 正常列
        for label, cell in zip(columns, row[1:]):
            points.append(Point(observed_at, kind, label, parse_number(cell)))
    return points


def parse_areaperc(body: bytes) -> tuple[list[Point], date | None]:
    """genloadareaperc.csv → Point，並回傳它帶的日期。

    ★ 這支是三支裡唯一帶完整日期的（另外兩支只有時分），所以拿它當
      另外兩支的日期基準——比用「今天」安全，午夜前後不會標錯日。
    """
    expected = len(PERC_COLUMNS) + 1
    points: list[Point] = []
    base_date: date | None = None
    for lineno, row in enumerate(_rows(body), start=1):
        if all(not cell.strip() for cell in row):
            continue
        if len(row) != expected:
            raise ParseError(
                f'area_perc 第 {lineno} 列欄數 {len(row)}，預期 {expected}'
                f'（來源可能改版了）：{row!r}')
        stamp = _parse_full_timestamp(row[0])
        base_date = stamp.date()
        for (kind, label), cell in zip(PERC_COLUMNS, row[1:]):
            points.append(Point(stamp, kind, label, parse_number(cell)))
    return points, base_date


def _parse_full_timestamp(raw: str) -> datetime:
    s = raw.strip()
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M'):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=TAIPEI)
        except ValueError:
            continue
    raise ParseError(f'genloadareaperc 的時戳認不得：{raw!r}')


def parse_loadpara(body: bytes, observed_at: datetime) -> list[Point]:
    """loadpara.json → kind='capacity' 的 Point。

    ★ 這個檔**沒有自己的時戳**（publish_time 是預估值的發布時間，不是
      curr_load 的時間）。所以 observed_at 由呼叫端給——用曲線的最新時點。

      這不是將就：2026-08-06 實測 curr_load 與同時點的能源別合計**完全相同**
      （40582 MW vs 40582 MW，差 0），證明兩者是同一瞬間的同一個量。
      cross_check_capacity() 每次執行都會重驗這件事。
    """
    try:
        doc = json.loads(body.decode('utf-8-sig'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParseError(f'loadpara.json 不是合法 JSON：{exc}') from None
    if not isinstance(doc, dict) or 'records' not in doc:
        raise ParseError(f'loadpara.json 結構不符預期（沒有 records）：{str(doc)[:120]}')

    # records 是「一組小 dict」而不是一個扁平物件，合併起來取用
    merged: dict[str, str] = {}
    for rec in doc['records']:
        if isinstance(rec, dict):
            merged.update(rec)

    points: list[Point] = []
    missing: list[str] = []
    for key, label in LOADPARA_FIELDS:
        if key not in merged:
            missing.append(key)
            continue
        points.append(Point(observed_at, 'capacity', label, parse_number(merged[key])))
    if missing:
        # ★ 欄位消失＝來源改版，要看得見。已取到的照樣回傳，不要整批丟掉。
        raise ParseError(f'loadpara.json 缺少預期欄位 {missing}（來源可能改版了）')
    return points


def cross_check_capacity(points: list[Point]) -> tuple[float, float] | None:
    """★ 第三條交叉檢查：loadpara 的即時用電必須等於同時點的能源別合計。

    兩者是不同來源檔對同一瞬間的描述，對不上就表示時點對錯了
    （例如 loadpara 已更新到下一個 10 分鐘，而曲線還沒）。

    回 (curr_load, 能源別合計)；缺任一邊回 None。
    """
    curr = next((p for p in points
                 if p.kind == 'capacity' and p.label == '即時用電'
                 and p.mw is not None), None)
    if curr is None:
        return None
    fuel_total = totals_at(points, 'fuel', curr.observed_at)
    if fuel_total is None:
        return None
    return curr.mw, fuel_total


def taipei_today() -> date:
    """★ 「今日」要用台北時區的今天。用 UTC 日期會在早上 8 點前錯一天。"""
    return datetime.now(TAIPEI).date()


def parse_all(fuel_body: bytes | None, area_body: bytes | None,
              perc_body: bytes | None,
              loadpara_body: bytes | None = None) -> list[Point]:
    """四支檔 → 全部 Point。任一支缺（沒抓到）就跳過那一支。"""
    perc_points: list[Point] = []
    base_date: date | None = None
    if perc_body is not None:
        perc_points, base_date = parse_areaperc(perc_body)
    if base_date is None:
        base_date = taipei_today()

    points: list[Point] = []
    if fuel_body is not None:
        points += parse_curve(fuel_body, 'fuel', FUEL_COLUMNS, base_date)
    if area_body is not None:
        points += parse_curve(area_body, 'area', AREA_COLUMNS, base_date)
    points += perc_points

    if loadpara_body is not None:
        # loadpara 沒有自己的時戳，掛在曲線最新的時點上（見 parse_loadpara）
        curve_times = [p.observed_at for p in points if p.kind == 'fuel']
        if curve_times:
            points += parse_loadpara(loadpara_body, max(curve_times))
        # 沒有曲線就沒有可信的時戳可掛——寧可不寫，也不要自己編一個時間
    return points


def totals_at(points: list[Point], kind: str,
              observed_at: datetime) -> float | None:
    """某個 kind 在某時點的合計 MW。

    ★ 含負值的儲能負載，不要濾掉也不要取絕對值——那是充電側，
      台電官方也把它畫成負值並計入合計。
    """
    values = [p.mw for p in points
              if p.kind == kind and p.observed_at == observed_at
              and p.mw is not None]
    return sum(values) if values else None


def cross_check(points: list[Point]) -> tuple[datetime, float, float] | None:
    """★ 最有價值的一條驗收：兩支曲線在最後一個共同時點的總和必須吻合。

    能源別與區域別是同一份用電的兩種切分，標錯或漏掉任何一欄，兩邊總和就會分岔。
    單看某一欄「看起來合理」驗不出欄序錯置（例如太陽能與重油對調，白天一樣有起伏）。

    回 (時點, 能源別合計, 區域別合計)；沒有共同時點回 None。
    """
    fuel_times = {p.observed_at for p in points if p.kind == 'fuel'}
    area_times = {p.observed_at for p in points if p.kind == 'area'}
    common = fuel_times & area_times
    if not common:
        return None
    latest = max(common)
    fuel_total = totals_at(points, 'fuel', latest)
    area_total = totals_at(points, 'area', latest)
    if fuel_total is None or area_total is None:
        return None
    return latest, fuel_total, area_total

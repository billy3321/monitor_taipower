"""來源檔的解析——純函式，不碰網路與資料庫。

欄序來源見 CLAUDE.md（2026-08-05 從官網現行 JS 分支的 balloon 文字逆向、
並用兩支曲線總和交叉驗證過）。改動這裡的欄位對應時 PARSER_VERSION 要跟著改。
"""
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
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

# ★★ 開放資料 d006001（unitdata.json）的「機組類型」→ 我們的欄位名稱。
#    兩邊指的是同一件事，只是台電自己在兩個地方用了不同寫法。
#    列在這裡的都是**已知等價**；沒列到的名稱一律當成「新類型」處理
#    （見 verify_fuel_columns），不會被猜著對應過去。
FUEL_TYPE_ALIASES = {'燃料油': '重油'}

# 逐機組資料要對得上曲線，兩邊同一類的合計差不得超過這個值（MW）。
#
# ★★ 1.0 不是「調到剛好會過」的數字，是**四捨五入的上界**：
#    曲線 CSV 的單位是萬瓩且只到小數一位（0.1 萬瓩 ＝ 1 MW），逐機組是
#    0.1 MW。同一個量在兩邊最多就差半格 ＝ 0.5 MW。2026-08-29 20:50 實測
#    12 類最大差 0.5（燃氣 15157.0 vs 15157.5），完全符合這個推導。
# ★ 所以這條檢查是**緊的**：真正的欄位位移會讓某一類差好幾千 MW，
#   在 1 MW 的門檻下無所遁形。放寬它就等於放棄這個檢查的全部價值——
#   哪天它開始跳，要查的是台電改了什麼，不是把數字調大。
UNIT_MATCH_TOLERANCE_MW = 1.0

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

# loadpara 的即時用電 vs 同時點能源別合計（實測差 0 MW）。
# 100 MW 是「嚴格吻合」：rehome 往回改掛時要求幾乎精確（實測 1、2、52 MW）。
CAPACITY_CHECK_TOLERANCE_MW = 100.0

# loadpara 有時比曲線最新一格更「新鮮」——它是即時值，CSV 是 10 分鐘切片，
# 爬升時段兩者可以差上百 MW（2026-08-14 兩次實測都是 171 MW）。
# 往回都找不到嚴格吻合、但最新格差距在此範圍內，就掛最新格：
# 時間誤差 <10 分鐘且方向是「值比標籤新」，比丟掉整個小時的點好。
# 不能再放寬：爬升時段相鄰兩格差 400–800 MW，300 仍分得出「新鮮」與「慢一格」。
CAPACITY_FRESH_TOLERANCE_MW = 300.0


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


def parse_unitdata(body: bytes) -> tuple[datetime, dict[str, float]]:
    """unitdata.json（開放資料 d006001）→ (時戳, {機組類型: 淨發電量 MW})。

    ★★ 這支是這批來源裡**唯一自己帶欄位名稱**的。另外兩支曲線 CSV 沒有標頭，
       12 欄／4 欄的意義只寫在圖表的 JavaScript 裡——原本我們把欄序寫死，
       等於假設台電永遠不改。而 load_fueltype_.html 裡第一欄的「核能」
       只是被 /* */ 註解掉，核能一旦重啟就會位移一格，我們會把每一種
       發電方式都標錯，**而圖表看起來完全正常**。

    ★ 單位：這支的「淨發電量(MW)」本來就是 MW，**不必**乘 WAN_KW_TO_MW。
      曲線 CSV 是萬瓩。兩邊單位不同是最容易寫錯的一格。

    ★ 機組類型欄實測夾著 HTML 殘渣（台電自己的 bug）：
      `儲能負載(Energy Storage System Load)</b>`。所以要先去標籤、
      再砍掉括號後的英文，最後查 FUEL_TYPE_ALIASES。
    """
    try:
        doc = json.loads(body.decode('utf-8-sig'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ParseError(f'unitdata.json 不是合法 JSON：{exc}') from None
    if not isinstance(doc, dict) or 'aaData' not in doc or 'DateTime' not in doc:
        raise ParseError('unitdata.json 結構不符預期（缺 DateTime 或 aaData）：'
                         f'{str(doc)[:120]}')

    stamp = _parse_full_timestamp(doc['DateTime'].replace('T', ' '))
    totals: dict[str, float] = {}
    for row in doc['aaData']:
        if not isinstance(row, dict) or '機組類型' not in row:
            raise ParseError(f'unitdata.json 的列缺少「機組類型」：{str(row)[:120]}')
        totals[normalise_fuel_type(row['機組類型'])] = (
            totals.get(normalise_fuel_type(row['機組類型']), 0.0)
            + (parse_unit_mw(row.get('淨發電量(MW)')) or 0.0))
    if not totals:
        raise ParseError('unitdata.json 一台機組都沒有（不等於全台停電）')
    return stamp, totals


def normalise_fuel_type(raw: str) -> str:
    """機組類型字串 → 我們的欄位名稱。砍掉括號後的英文，再查別名表。

    ★ 實測有一個值夾著 HTML 殘渣（台電自己的 bug）：
      `儲能負載(Energy Storage System Load)</b>`。**括號切割本來就吃掉它了**，
      所以這裡不另外去標籤——加一條沒有測試蓋得到的正則只是臆測。

    ★ 萬一哪天冒出括號**前**就有標籤的寫法，失敗方向是安全的：它會變成一個
      「我們沒有欄位的類型」，verify_fuel_columns 會大聲擋下來，不會被默默
      正規化成某個既有類別。寧可假警報，不要靜默錯標。"""
    s = (raw or '').split('(')[0].strip()
    return FUEL_TYPE_ALIASES.get(s, s)


def parse_unit_mw(raw: str | None) -> float | None:
    """逐機組的「淨發電量(MW)」→ float。★ 已經是 MW，不再換算。

    ★ 認不得的字面回 None 而不是丟例外——**與曲線 CSV 的 parse_number 不同**。
      理由：這支是拿來對帳的旁證，單一機組欄位髒掉不該讓整次抓取失敗；
      而曲線 CSV 是資料本體，那裡認不得就必須停下來。
    """
    s = (raw or '').strip().replace(',', '')
    if s in NULL_TOKENS:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def verify_fuel_columns(points: list[Point], stamp: datetime,
                        unit_totals: dict[str, float]) -> list[str]:
    """拿逐機組資料按**名字**驗能源別 12 欄的對應。回傳問題清單（空＝通過）。

    ★★ 這是「不賭欄序」的關鍵一步。原本欄序是寫死的假設、沒有任何東西在
       檢查；現在每次跑都拿台電自己帶名稱的資料一類一類對帳，對不上就
       **不寫**並指名是哪一類。核能重啟那天，第一次跑就會擋下來。

    ★ 已知限制，刻意不掩蓋：晚上「太陽能」與「儲能負載」都是 0，那個瞬間
      光看數值分不出這兩欄，對調了也驗得過。但一天跑 25 次、其中十幾次在
      白天，太陽能一有出力就分得出來——**這個模糊窗每天自己會關**。
      所以這支的語意是「這一輪沒有發現不一致」，不是「永遠保證正確」。
    """
    curve = {p.label: p.mw for p in points
             if p.kind == 'fuel' and p.observed_at == stamp}
    if not curve:
        return []                       # 沒有共同時點就不下判斷（未知≠不一致）

    problems: list[str] = []
    # ① 出現我們沒有欄位的類型 → 幾乎確定是欄位改版，最該擋下來的情況
    unknown = sorted(set(unit_totals) - set(FUEL_COLUMNS))
    if unknown:
        problems.append(f'逐機組出現我們沒有欄位的類型 {unknown}'
                        f'（欄位改版？我們的 12 欄是 {FUEL_COLUMNS}）')
    # ② 逐類比對
    for label in FUEL_COLUMNS:
        got, want = curve.get(label), unit_totals.get(label)
        if got is None or want is None:
            problems.append(f'{label}：曲線={got}、逐機組={want}（其中一邊沒有）')
        elif abs(got - want) >= UNIT_MATCH_TOLERANCE_MW:
            problems.append(f'{label}：曲線={got:.1f}、逐機組={want:.1f}'
                            f'（差 {got - want:+.1f} MW）')
    return problems


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


def rehome_capacity(points: list[Point], max_back: int = 6
                    ) -> tuple[list[Point], datetime, float] | None:
    """把 capacity 掛到「即時用電＝能源別合計」成立的時點上。

    ★ 為什麼需要：loadpara 偶爾比曲線**慢**。2026-08-10 實測慢一格
      （早上爬升時段一格差 462 MW）；2026-08-13 實測慢到**五格**
      （18:55 抓到的值精確吻合 18:00 的合計，差 2 MW）。慢不是錯誤，
      掛回值真正對應的時點就好——所以 max_back 預設 6（一小時）。

    兩段式判定，**嚴格優先**：
      1. 從最新往回 max_back 格找嚴格吻合（< CAPACITY_CHECK_TOLERANCE_MW）。
         夜間平坦時多格都吻合，取最新的（loadpara 是「當下」的值）。
      2. 都沒有，但最新格差距 < CAPACITY_FRESH_TOLERANCE_MW → 掛最新格。
         這是「loadpara 比 CSV 新鮮」的情況（2026-08-14 兩次實測 171 MW），
         值介於最新格與下一格之間，時間誤差 <10 分鐘。

    回 (改掛後的 points, 掛載時點, 差值)；兩段都對不上回 None——那是真的
    不同步（來源改版、單位錯），呼叫端要丟掉 capacity 並記錯誤。
    ★ 即時用電本身是 None（未報告）時也回 None：驗不了時點的 capacity
      寧可不寫，不要掛在猜的時間上。
    """
    curr = next((p for p in points
                 if p.kind == 'capacity' and p.label == '即時用電'
                 and p.mw is not None), None)
    if curr is None:
        return None
    fuel_times = sorted({p.observed_at for p in points if p.kind == 'fuel'})
    if not fuel_times:
        return None

    def rehomed_at(t: datetime, diff: float):
        if t == curr.observed_at:
            return points, t, diff
        return ([replace(p, observed_at=t) if p.kind == 'capacity' else p
                 for p in points], t, diff)

    for t in reversed(fuel_times[-(max_back + 1):]):
        total = totals_at(points, 'fuel', t)
        if total is None:
            continue
        diff = abs(curr.mw - total)
        if diff < CAPACITY_CHECK_TOLERANCE_MW:
            return rehomed_at(t, diff)

    latest = fuel_times[-1]
    total = totals_at(points, 'fuel', latest)
    if total is not None:
        diff = abs(curr.mw - total)
        if diff < CAPACITY_FRESH_TOLERANCE_MW:
            return rehomed_at(latest, diff)
    return None


def diagnose_sides(points: list[Point]
                   ) -> tuple[datetime, float | None, float | None] | None:
    """兩支曲線各自離 loadpara 的即時用電多遠。回 (時點, 能源別偏離, 區域別偏離)。

    ★★ 為什麼需要：兩支曲線總和對不上時，光看差值**不知道是哪一邊壞了**。
       loadpara 的即時用電是**獨立的第三個檔**，可以當裁判。
       實測正式庫 2026-08-19~28 共 197 個錨點，兩側都在 **2 MW 以內**；
       08-29 區域別仍是 2 MW、能源別跑到 46 MW——**乾淨地指出是能源別那側**。

    ★ 時點取「兩支曲線都有值的最新共同時點」，不是 loadpara 自己宣稱的時間。
      loadpara 有時比曲線快或慢（見 rehome_capacity），但那個時間誤差
      **對兩側是同一個**，所以拿來比「誰離得比較遠」仍然成立。
      ★ 這一點很重要：診斷必須在**壞掉的時候**算得出來。如果改成依賴
        rehome 成功（而 rehome 是拿能源別去錨的），能源別正是壞掉那側時
        rehome 會失敗，於是最需要診斷的那一刻反而沒有診斷。"""
    curr = next((p for p in points
                 if p.kind == 'capacity' and p.label == '即時用電'
                 and p.mw is not None), None)
    if curr is None:
        return None
    common = ({p.observed_at for p in points if p.kind == 'fuel'}
              & {p.observed_at for p in points if p.kind == 'area'})
    if not common:
        return None
    at = max(common)
    ftot = totals_at(points, 'fuel', at)
    atot = totals_at(points, 'area', at)
    return (at,
            None if ftot is None else abs(ftot - curr.mw),
            None if atot is None else abs(atot - curr.mw))


def name_the_drifting_side(diag: tuple[datetime, float | None, float | None] | None,
                           tolerance: float = CAPACITY_CHECK_TOLERANCE_MW) -> str:
    """把 diagnose_sides 的數字講成一句人話，寫進 note 用。

    ★ 只在**兩側偏離差距明顯**時才指名，否則說「判不出來」。
      指錯邊比不指還糟：會叫人去修沒有壞的那一邊。
    """
    if diag is None:
        return '無即時用電可比對'
    at, fdev, adev = diag
    if fdev is None or adev is None:
        return '缺一側資料'
    if fdev < tolerance and adev < tolerance:
        return f'兩側都貼齊即時用電(能源別{fdev:.0f}/區域別{adev:.0f} MW)'
    if fdev >= tolerance > adev:
        return f'能源別偏離即時用電 {fdev:.0f} MW(區域別僅 {adev:.0f})'
    if adev >= tolerance > fdev:
        return f'區域別偏離即時用電 {adev:.0f} MW(能源別僅 {fdev:.0f})'
    return f'兩側都偏離即時用電(能源別{fdev:.0f}/區域別{adev:.0f} MW)'


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


def worst_divergence(points: list[Point]) -> float:
    """當天所有共同時點裡，能源別合計 − 區域別合計的**絕對值最大**那一個（帶正負）。

    ★ 為什麼要記這個而不是只記最新時點：交叉檢查從 2026-08-29 起不再擋資料，
      分岔就必須留在看得見的地方，否則等於「調寬門檻讓異常消失」。
      最新那一點可能剛好吻合（實測 123 個時點裡有 31 個是吻合的），
      只看它會讓一整天 75% 的時點分岔完全不出現在紀錄上。
    """
    fuel: dict[datetime, float] = {}
    area: dict[datetime, float] = {}
    for p in points:
        if p.mw is None:
            continue
        if p.kind == 'fuel':
            fuel[p.observed_at] = fuel.get(p.observed_at, 0.0) + p.mw
        elif p.kind == 'area':
            area[p.observed_at] = area.get(p.observed_at, 0.0) + p.mw
    diffs = [fuel[t] - area[t] for t in fuel.keys() & area.keys()]
    return max(diffs, key=abs) if diffs else 0.0


def taipei_today() -> date:
    """★ 「今日」要用台北時區的今天。用 UTC 日期會在早上 8 點前錯一天。"""
    return datetime.now(TAIPEI).date()


def parse_files(bodies: dict[str, bytes]) -> tuple[list[Point], list[str]]:
    """曲線各檔各自解析，**單檔失敗不拖垮其他檔**——與抓取失敗同一條原則：
    解析失敗的檔 ≈ 沒抓到的檔，跳過它、記一筆錯誤、其他檔照常處理。

    ★ 這是正式執行路徑用的。一個大 try 包住全部的寫法踩過的坑：
      loadpara.json 哪天改個欄位，連好好的 fuel/area 曲線都一起陪葬。

    回 (points, 錯誤訊息列表)。錯誤列表非空時呼叫端要把它變成可見失敗。
    """
    points: list[Point] = []
    errors: list[str] = []

    perc_points: list[Point] = []
    base_date: date | None = None
    if 'genloadareaperc.csv' in bodies:
        try:
            perc_points, base_date = parse_areaperc(bodies['genloadareaperc.csv'])
        except ParseError as exc:
            errors.append(f'genloadareaperc.csv 解析失敗：{exc}')
    if base_date is None:
        base_date = taipei_today()

    for name, kind, cols in (('loadfueltype.csv', 'fuel', FUEL_COLUMNS),
                             ('loadareas.csv', 'area', AREA_COLUMNS)):
        if name not in bodies:
            continue
        try:
            points += parse_curve(bodies[name], kind, cols, base_date)
        except ParseError as exc:
            errors.append(f'{name} 解析失敗：{exc}')
    points += perc_points

    if 'loadpara.json' in bodies:
        # loadpara 沒有自己的時戳，掛在曲線最新的時點上（見 parse_loadpara）。
        # 沒有能源別曲線就沒有可信的時戳可掛，也做不了 capacity 交叉檢查——
        # 寧可不寫，也不要自己編一個時間。
        fuel_times = [p.observed_at for p in points if p.kind == 'fuel']
        if fuel_times:
            try:
                points += parse_loadpara(bodies['loadpara.json'], max(fuel_times))
            except ParseError as exc:
                errors.append(f'loadpara.json 解析失敗：{exc}')
    return points, errors


def parse_all(fuel_body: bytes | None, area_body: bytes | None,
              perc_body: bytes | None,
              loadpara_body: bytes | None = None) -> list[Point]:
    """嚴格版：四支曲線檔 → 全部 Point，任何一支解析失敗就丟例外。
    ★ 不含 unitdata.json——那支是驗欄位用的旁證，不產生 Point。

    給測試與驗收腳本用。正式執行路徑用 parse_files()——單檔失敗要隔離。
    """
    bodies = {name: body for name, body in (
        ('loadfueltype.csv', fuel_body), ('loadareas.csv', area_body),
        ('genloadareaperc.csv', perc_body), ('loadpara.json', loadpara_body),
    ) if body is not None}
    points, errors = parse_files(bodies)
    if errors:
        raise ParseError('; '.join(errors))
    return points


# 跨午夜防線的容忍值：台電發布延遲 7–11 分鐘，正常情況最新點永遠在過去。
FUTURE_TOLERANCE = timedelta(minutes=20)


def find_future_points(points: list[Point], now: datetime) -> list[Point]:
    """回「在未來」的點——正常情況必為空。

    ★ 什麼時候會非空：慢速抓取**跨過午夜**時。fuel 抓到的是舊日滿檔、
      之後抓的 genloadareaperc 已換日，日期基準取自後者，舊日的 23:50
      就會被標成新一天的 23:50——那是快 24 小時後的未來。寫進去的話，
      整天份的假資料會躺在圖上，等著被之後的執行一小時一小時慢慢蓋掉。

    呼叫端看到非空就該整批拒寫：23:55 那次已經把舊日收乾淨了，
    這一批丟掉沒有損失。
    """
    cutoff = now + FUTURE_TOLERANCE
    return [p for p in points if p.observed_at > cutoff]


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

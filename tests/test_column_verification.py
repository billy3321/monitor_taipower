"""欄位對應要靠**名字**驗，不是靠順序猜（2026-08-29）。

★★ 這組測試守的是這個專案最危險的失敗形狀：曲線 CSV **沒有標頭列**，
   12 欄／4 欄的意義只寫在圖表的 JavaScript 裡。在此之前 parser.py 把欄序
   寫死，等於假設台電不會改——而 load_fueltype_.html 裡第一欄的「核能」
   只是被 /* */ 註解掉而已。核能一旦重啟、註解拿掉，12 欄全部位移一格，
   我們會把每一種發電方式都標錯，**而圖表看起來完全正常**，
   可能好幾個月沒有人發現。

   開放資料 d006001（unitdata.json）是這批來源裡唯一自己帶欄位名稱的，
   所以拿它按「機組類型」逐類對帳。

fixtures/20260829/ 是 2026-08-29 20:50 同一輪抓下來的**真實檔**
（unitdata 與 genloadareaperc 時戳都是 20:50，所以驗得起來）。
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json

import pytest

from taipower_curve import parser as P
from taipower_curve import state

FIX = Path(__file__).parent / 'fixtures' / '20260829'
CURVE_FILES = ['loadfueltype.csv', 'loadareas.csv', 'genloadareaperc.csv']


def body(name: str) -> bytes:
    return (FIX / name).read_bytes()


@pytest.fixture
def points():
    pts, errs = P.parse_files({n: body(n) for n in CURVE_FILES})
    assert errs == [], f'fixture 本身不該解析失敗：{errs}'
    return pts


@pytest.fixture
def unit():
    return P.parse_unitdata(body('unitdata.json'))


# ── 逐機組資料的解析 ────────────────────────────────────────────

def test_unitdata_gives_exactly_our_twelve_categories(unit):
    """★ 台電自己報出來的類型，必須剛好是我們有欄位的那 12 種。

    多出來的類型＝欄位改版（核能重啟就是這個長相）；少掉的也一樣要看得見。
    """
    _, totals = unit
    assert sorted(totals) == sorted(P.FUEL_COLUMNS)


def test_unitdata_timestamp_is_taipei_aware(unit):
    stamp, _ = unit
    assert stamp == datetime(2026, 8, 29, 20, 50, tzinfo=P.TAIPEI)
    assert stamp.tzinfo is not None, '時戳一定要帶時區，裸的會在午夜前後標錯日'


def test_paren_suffix_is_stripped_including_taipower_own_html_bug():
    """★ 機組類型欄實測夾著 HTML 殘渣（台電自己的 bug）：
    `儲能負載(Energy Storage System Load)</b>`。

    ★ 這裡**沒有**另外寫一條去 HTML 標籤的正則：括號切割本來就吃掉它了，
      加了也沒有測試蓋得到（實測 12 種類型只有這一個帶標籤，而且在括號後）。
      第一版寫了那條正則，突變測試把它拿掉之後測試照樣全綠——那就是
      **測試在說謊**的訊號，所以正則被移除、換成這條說實話的測試。

    ★ 萬一哪天出現括號前就有標籤的寫法，它會變成「我們沒有欄位的類型」
      被 verify 大聲擋下來——假警報好過靜默錯標。"""
    raw = [r['機組類型'] for r in json.loads(
        body('unitdata.json').decode('utf-8-sig'))['aaData']]
    residue = [x for x in raw if '<' in x]
    assert residue, 'fixture 應該保留那個 HTML 殘渣'
    assert all('(' in x and x.index('<') > x.index('(') for x in residue), \
        '★ 前提是標籤都在括號後面。哪天不是了這條會紅，去看 normalise_fuel_type'
    assert P.normalise_fuel_type('儲能負載(Energy Storage System Load)</b>') == '儲能負載'
    assert P.normalise_fuel_type('<b>核能</b>') == '<b>核能</b>', \
        '括號前的標籤刻意不處理——留給 verify 當成未知類型擋下來'


def test_fuel_type_alias_is_explicit_not_guessed():
    """★ 台電在兩個地方對同一件事用了不同寫法（逐機組叫「燃料油」、
    曲線叫「重油」）。這種等價**只能明列**，不可以用相似度猜——
    猜錯就是把兩種燃料的資料混在一起，而畫面完全正常。"""
    assert P.normalise_fuel_type('燃料油') == '重油'
    assert P.normalise_fuel_type('核能') == '核能', '沒列在別名表的一律原樣留著'


def test_unit_mw_is_already_mw_not_wan_kw(unit):
    """★★ 單位陷阱：曲線 CSV 是萬瓩（要 ×10），逐機組是 MW（不可再乘）。
    寫錯這一格，對帳會全部差十倍而且看起來像「欄位全錯」。"""
    _, totals = unit
    assert 10_000 < sum(totals.values()) < 60_000, \
        f'全台用電應在數萬 MW 量級，得到 {sum(totals.values())}'


# ── 驗證本身 ────────────────────────────────────────────────────

def test_real_data_passes_column_verification(points, unit):
    stamp, totals = unit
    assert P.verify_fuel_columns(points, stamp, totals) == []


def test_detects_a_shifted_column_map(points, unit):
    """★★ 核能重啟那天的長相：台電在最前面插一欄，12 欄整排位移。

    這裡用**真實資料**造：把逐機組的合計往後移一格、最前面放進核能。
    每一類的數字都會對到隔壁那一類，12 欄全部報錯——這正是我們要的，
    因為那時候寫進資料庫的每一筆都會是錯的標籤。
    """
    stamp, totals = unit
    shifted = {'核能': 2800.0}
    for i, label in enumerate(P.FUEL_COLUMNS[:-1]):
        shifted[P.FUEL_COLUMNS[i + 1]] = totals[label]

    problems = P.verify_fuel_columns(points, stamp, shifted)
    assert problems, '欄位整排位移必須被擋下來'
    assert any('核能' in p for p in problems), \
        '要指名是哪個新類型冒出來，不能只說「對不上」'
    assert len(problems) >= 6, f'整排位移應該多類報錯，只得到 {problems}'


def test_detects_one_category_going_wrong(points, unit):
    """單一類別對不上也要抓到——不是只有整排位移才算。"""
    stamp, totals = unit
    tweaked = dict(totals)
    tweaked['太陽能'] = tweaked['太陽能'] + 500.0
    problems = P.verify_fuel_columns(points, stamp, tweaked)
    assert len(problems) == 1 and '太陽能' in problems[0], problems
    assert '-500' in problems[0].replace('−', '-'), \
        f'要寫出差多少，光說「不一致」查起來沒用：{problems[0]}'


def test_rounding_alone_never_trips_the_check(points, unit):
    """★ 門檻 1.0 MW 是四捨五入的上界（曲線只到 0.1 萬瓩＝1 MW，
    逐機組到 0.1 MW，同一個量最多差半格）。半格以內不可以報錯，
    否則這條檢查會天天誤報，然後被人調寬——調寬就等於廢掉它。"""
    stamp, totals = unit
    jittered = {k: v + 0.49 for k, v in totals.items()}
    assert P.verify_fuel_columns(points, stamp, jittered) == []


def test_no_common_timestamp_means_unknown_not_inconsistent(points, unit):
    """★ 未知≠不一致。曲線還沒出到逐機組那個時點時，不可以判成欄位錯——
    那會在每天午夜前後製造假失敗。"""
    _, totals = unit
    far_future = datetime(2030, 1, 1, tzinfo=P.TAIPEI)
    assert P.verify_fuel_columns(points, far_future, totals) == []


# ── 分岔紀錄 ────────────────────────────────────────────────────

def test_worst_divergence_reports_the_whole_day_not_just_the_latest(points):
    """★★ 交叉檢查 2026-08-29 起降級成「記下來」而不是「擋下來」，
    所以分岔必須留在看得見的地方。

    只看最新時點會漏掉一整天：這份 fixture 最新時點幾乎吻合，
    但當天最大分岔是 −336 MW（台電自己的能源別合計偏低）。
    """
    assert P.worst_divergence(points) == pytest.approx(-336.0, abs=1.0)


def test_worst_divergence_is_zero_when_nothing_to_compare():
    assert P.worst_divergence([]) == 0.0


# ── 降級不可以變成常態 ──────────────────────────────────────────

def test_unverified_age_is_unknown_before_first_success(tmp_path, monkeypatch):
    """★ 沒驗過回 None＝「未知」，呼叫端不可以當成「剛驗過」。
    第一次部署、或有人清掉 data/ 都會這樣。"""
    monkeypatch.setattr(state, 'LAST_VERIFIED', tmp_path / 'x.txt')
    assert state.unverified_for(datetime.now(timezone.utc)) is None


def test_unverified_age_measures_time_not_run_count(tmp_path, monkeypatch):
    """★★ 記「上次成功驗證的時間」而不是「連續退回幾次」：
    次數會被機器關機騙過去（沒跑就不累加，時間卻照走）。"""
    monkeypatch.setattr(state, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(state, 'LAST_VERIFIED', tmp_path / 'x.txt')
    t0 = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
    state.mark_verified(t0)
    assert state.unverified_for(t0 + timedelta(hours=2)) == timedelta(hours=2)
    assert state.unverified_for(t0 + timedelta(hours=30)) > state.MAX_UNVERIFIED


def test_corrupt_state_file_is_treated_as_unknown(tmp_path, monkeypatch):
    p = tmp_path / 'x.txt'
    p.write_text('這不是時間', encoding='utf-8')
    monkeypatch.setattr(state, 'LAST_VERIFIED', p)
    assert state.unverified_for(datetime.now(timezone.utc)) is None


# ── 兩支曲線對不上時：判得出是哪一側 ────────────────────────────

def _with_capacity(points, at, mw):
    """把一個「即時用電」點掛在指定時點上（模擬 loadpara）。"""
    return points + [P.Point(at, 'capacity', '即時用電', mw)]


def test_loadpara_names_the_drifting_side(points):
    """★★ 兩支曲線總和對不上時，光看差值**不知道是哪一邊壞了**。
    loadpara 的即時用電是獨立的第三個檔，可以當裁判。

    實測正式庫 2026-08-19~28 共 197 個錨點兩側都在 2 MW 內；08-29 區域別
    仍是 2 MW、能源別跑到 46 MW——乾淨地指出是能源別那側。
    """
    at = max({p.observed_at for p in points if p.kind == 'fuel'}
             & {p.observed_at for p in points if p.kind == 'area'})
    area_total = P.totals_at(points, 'area', at)

    # 即時用電貼著區域別（實測就是這樣）→ 應該指名能源別
    diag = P.diagnose_sides(_with_capacity(points, at, area_total))
    verdict = P.name_the_drifting_side(diag)
    _, fdev, adev = diag
    assert adev < 1.0, f'區域別應該貼齊即時用電，得到 {adev}'
    if fdev >= P.CAPACITY_CHECK_TOLERANCE_MW:
        assert '能源別偏離' in verdict, verdict


def test_diagnosis_points_at_whichever_side_is_off(points):
    """反面：換成**區域別**壞掉，就該指名區域別。

    ★ 判準不可以寫死成「永遠怪能源別」——今天是它，明天可能是另一邊。
      第一版這條寫成「指名區域別 or 兩側都貼齊」，於是把 name_the_drifting_side
      的區域別分支改成回傳「能源別」之後，測試照樣全綠（最新時點本來就常常
      兩側都吻合，走的是「都貼齊」那條）。**選言的斷言會放過突變**——
      改成造一個區域別確實偏離的情境，只接受一個答案。"""
    at = max({p.observed_at for p in points if p.kind == 'fuel'}
             & {p.observed_at for p in points if p.kind == 'area'})
    fuel_total = P.totals_at(points, 'fuel', at)
    broken_area = [P.Point(p.observed_at, p.kind, p.label,
                           None if p.mw is None else p.mw * 0.9)
                   if p.kind == 'area' else p for p in points]
    diag = P.diagnose_sides(_with_capacity(broken_area, at, fuel_total))
    _, fdev, adev = diag
    assert fdev < P.CAPACITY_CHECK_TOLERANCE_MW <= adev, (fdev, adev)
    assert '區域別偏離' in P.name_the_drifting_side(diag)


def test_diagnosis_works_even_when_fuel_is_the_broken_side(points):
    """★★ 最重要的一條：診斷必須在**壞掉的時候**算得出來。

    rehome_capacity 是拿能源別去錨的，能源別正是壞掉那側時 rehome 會失敗。
    diagnose_sides 刻意不依賴 rehome——它取「兩支曲線都有值的最新共同時點」，
    時間誤差對兩側是同一個，比「誰離得遠」仍然成立。
    """
    at = max({p.observed_at for p in points if p.kind == 'fuel'}
             & {p.observed_at for p in points if p.kind == 'area'})
    area_total = P.totals_at(points, 'area', at)
    broken = [P.Point(p.observed_at, p.kind, p.label,
                      None if p.mw is None else p.mw * 0.9)
              if p.kind == 'fuel' else p for p in points]
    pts = _with_capacity(broken, at, area_total)
    assert P.rehome_capacity(pts) is None, '前提：能源別壞成這樣 rehome 應該失敗'
    verdict = P.name_the_drifting_side(P.diagnose_sides(pts))
    assert '能源別偏離' in verdict, f'rehome 失敗時仍要判得出來，得到 {verdict}'


def test_no_capacity_means_cannot_judge_not_all_clear(points):
    """★ 沒有即時用電時要說「無法比對」，不可以回一句像通過的話。"""
    assert P.diagnose_sides(points) is None
    assert P.name_the_drifting_side(None) == '無即時用電可比對'

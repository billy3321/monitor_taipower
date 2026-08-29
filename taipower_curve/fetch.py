"""抓三支 CSV。

★ 用 requests 不 shell out 去呼叫 curl；RelaxedGovTwAdapter 直接沿用
  scripts/preflight.py 的定義（單向依賴，preflight 保持零本專案相依、
  隨時能獨立診斷；verify_fixtures.py 已經是這個用法）。
"""
from dataclasses import dataclass
from pathlib import Path
import json
import sys
import time

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
from preflight import RelaxedGovTwAdapter  # noqa: E402

BASE = 'https://www.taipower.com.tw/d006/loadGraph/loadGraph/data'

# ★ Referer 用「那個頁面本來就會載入這支 CSV 的網址」，不要亂填。
#   能源別是 10264、區域別是 10263；占比檔兩頁都會載，掛在能源別頁下。
_PAGE = 'https://www.taipower.com.tw/2289/2363/2367/2368/{}/normalPost'
REFERERS = {
    'loadfueltype.csv': _PAGE.format(10264),
    'loadareas.csv': _PAGE.format(10263),
    'genloadareaperc.csv': _PAGE.format(10264),
    'loadpara.json': _PAGE.format(10264),
    'unitdata.json': _PAGE.format(10264),
}

# ★★ 不同主機的檔要走這裡。曲線那四支在 www.taipower.com.tw 的圖表資料目錄下，
#    unitdata.json 是**開放資料平台**（service.taipower.com.tw）的
#    d006001「各機組發電量」——它是這批來源裡**唯一自己帶欄位名稱**的。
#
# ★★ 它存在的理由：另外那三支 CSV **沒有標頭列**，12 欄／4 欄各是什麼意思
#    只寫在圖表的 JavaScript 裡。原本我們把欄序寫死在 parser.py，那是
#    「假設台電不會改」——而 load_fueltype_.html 裡第一欄的「核能」
#    只是被 /* */ 註解掉而已，核能一旦重啟、註解拿掉，12 欄全部位移一格，
#    我們會把每一種發電方式都標錯，**而圖表看起來完全正常**。
#    改成每次跑都拿這支按名字對帳，欄序就不再是假設而是**每次驗過的事實**。
#
# ★ 為什麼不乾脆全部改用這支：它是**單一時點的快照**，不是曲線。
#   CSV 是「今天到目前為止的完整檔」，一次抓成功就把整天補回來——
#   2026-08-29 就是靠這個特性，失敗十幾次仍然沒掉今天的資料。
#   改成純輪詢就失去回補能力，漏一次就永遠少那幾個點。
# ★ 同平台的 d006010（逐機組歷史曲線）**刻意不抓**：189 MB、落後約四個月、
#   而且口徑不同（只有台電自有機組，沒有民營電廠，類別也只有 10 種），
#   拿來對帳會得到系統性偏低的假分岔。
_OPENDATA = 'https://service.taipower.com.tw/data/opendata/apply/file'
URLS = {
    'unitdata.json': f'{_OPENDATA}/d006001/001.json',
}
# 期望的內容形態。★ content-type 與內容都要驗：回 200 但吐挑戰頁（HTML）
#   必須當成失敗，不是「今天沒資料」。
EXPECTED = {
    'loadfueltype.csv': 'csv',
    'loadareas.csv': 'csv',
    'genloadareaperc.csv': 'csv',
    'loadpara.json': 'json',
    'unitdata.json': 'json',
}
FILES = list(REFERERS)


def source_url(name: str) -> str:
    """抓下來的資料要帶原始網址（平台紀律），歸檔與 fetch_run 都用這個。

    ★ 大部分檔在 BASE 底下，但開放資料平台那支在另一個主機，走 URLS 覆寫。
    """
    return URLS.get(name) or f'{BASE}/{name}'


class FetchError(Exception):
    """抓取失敗。http_status 是實際拿到的狀態碼；連不上時是 None。"""

    def __init__(self, message: str, http_status: int | None = None):
        super().__init__(message)
        self.http_status = http_status


@dataclass
class FetchResult:
    bodies: dict[str, bytes]            # 檔名 → 內容（只放成功的）
    errors: dict[str, str]              # 檔名 → 失敗原因
    http_status: int | None             # 給 monitor_fetch_run 記錄用

    @property
    def ok(self) -> bool:
        return not self.errors


def build_headers(user_agent: str, referer: str) -> dict[str, str]:
    """★ 送完整的一組標頭，不是只有 UA。

    目的不是騙過誰，是讓我們的請求跟那個頁面自己發的請求長得一樣——
    只帶 UA 的請求在真實瀏覽器裡根本不存在，WAF 對那種請求常會提高警覺。

    ★ 但這對「被 CloudFront 依 IP／ASN 封鎖」完全無效（三台 GCP 全試過）。
      標頭只讓請求正常，不是拿來規避封鎖的；抓不到就讓它失敗、讓告警響。
    """
    return {
        'User-Agent': user_agent,
        'Accept': 'text/csv,text/plain,*/*',
        'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
        'Referer': referer,
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
    }


def make_session() -> requests.Session:
    s = requests.Session()
    # ★ 台灣政府 PKI 缺 Subject Key Identifier，Python 3.13+ 預設會拒絕。
    #   只放寬 X509 strict，其餘驗證全保留——絕不可改成 verify=False。
    s.mount('https://', RelaxedGovTwAdapter())
    return s


def fetch_one(session: requests.Session, name: str, user_agent: str,
              timeout: float = 25.0) -> bytes:
    url = source_url(name)
    try:
        r = session.get(url, headers=build_headers(user_agent, REFERERS[name]),
                        timeout=timeout)
    except requests.exceptions.SSLError as exc:
        raise FetchError(
            f'{name}: TLS 失敗（不是被封鎖，換網路沒有用；'
            f'確認 RelaxedGovTwAdapter 有生效）— {str(exc)[:120]}') from exc
    except requests.exceptions.RequestException as exc:
        raise FetchError(f'{name}: {type(exc).__name__} — {str(exc)[:120]}') from exc

    if r.status_code != 200:
        raise FetchError(f'{name}: HTTP {r.status_code}'
                         '（很可能是這台機器的出口 IP 被 CloudFront 擋了）',
                         http_status=r.status_code)
    _validate(name, r)
    return r.content


def _validate(name: str, r: requests.Response) -> None:
    """★ 空回應／挑戰頁不等於「今天沒資料」，一律當失敗。

    content-type 與內容都要驗——只看其中一個都會被騙：
      - 只看狀態碼：CloudFront 擋頁是 HTTP 200 + HTML（踩過的坑）
      - 只看 content-type：擋頁有時仍標成 text/csv
    """
    kind = EXPECTED[name]
    ctype = (r.headers.get('content-type') or '').lower()
    head = r.content[:400].lstrip().lower()

    if not r.content.strip():
        raise FetchError(f'{name}: 回應是空的（不等於今天沒資料）',
                         http_status=r.status_code)
    if 'html' in ctype or head.startswith(b'<!doctype') or b'<html' in head:
        raise FetchError(
            f'{name}: HTTP 200 但內容是 HTML（content-type={ctype or "無"}）'
            '——挑戰頁／擋頁，這是靜默失敗的典型長相',
            http_status=r.status_code)
    if kind == 'json':
        if 'json' not in ctype and not head.startswith((b'{', b'[')):
            raise FetchError(f'{name}: 期望 JSON，content-type={ctype or "無"}、'
                             f'開頭是 {r.content[:40]!r}', http_status=r.status_code)
        try:
            json.loads(r.content.decode('utf-8-sig'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FetchError(f'{name}: content-type 是 JSON 但解不開——{exc}',
                             http_status=r.status_code) from None
    else:                                   # csv
        if 'json' in ctype:
            raise FetchError(f'{name}: 期望 CSV 卻拿到 {ctype}',
                             http_status=r.status_code)


def fetch_all(user_agent: str, delay: float = 1.0) -> FetchResult:
    """抓五支檔。★ 每次執行只打 5 個請求、之間 sleep，不做重試風暴。

    第五支是驗欄位用的 unitdata.json（開放資料，另一個主機）。
    ★ 它抓不到不該讓曲線一起失敗——errors 逐檔記錄，呼叫端自己決定，
      run_once 對它的處理是「本次欄位未驗證」而不是「本次失敗」。
    """
    session = make_session()
    bodies: dict[str, bytes] = {}
    errors: dict[str, str] = {}
    status: int | None = None
    for i, name in enumerate(FILES):
        if i:
            time.sleep(delay)               # 爬取自律
        try:
            bodies[name] = fetch_one(session, name, user_agent)
            status = status or 200
        except FetchError as exc:
            errors[name] = str(exc)
            if exc.http_status is not None:
                status = exc.http_status    # 失敗的碼比 200 有診斷價值
    return FetchResult(bodies=bodies, errors=errors, http_status=status)

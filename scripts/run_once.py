#!/usr/bin/env python3
"""進入點——目前只做前置檢查（**抓取與寫入的實作留給那台機器上的 AI**，見 CLAUDE.md）。

先跑這支。三個檔都 ✓ 才有後面的事。

★ 這支存在的理由：這個專案的全部前提是「這台機器抓得到台電官網」。
  但「抓不到」有兩種完全不同的原因，訊息一定要分清楚，否則會叫人去修錯的東西：

  1. **TLS：Missing Subject Key Identifier**
     台灣政府 PKI 普遍缺這個欄位，Python 3.13 預設的 VERIFY_X509_STRICT 會拒絕。
     這**不是**被封鎖，換網路沒有用。解法是放寬 X509 strict（下面的
     relaxed_gov_tw_context），curl 沒有這項檢查所以 curl 測起來是好的——
     這正是最容易誤判的地方。
  2. **CloudFront 403 / 回 200 但內容是 HTML**
     這才是出口 IP 被歸類成雲端／資料中心網段。換機器或換網路。

★ 絕對不要改成 verify=False 來「解決」第 1 種——那會連憑證鏈與 hostname
  驗證一起關掉。下面的 assert 就是防止有人日後偷改。
"""
import ssl
import sys

import requests
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager

BASE = 'https://www.taipower.com.tw/d006/loadGraph/loadGraph/data'
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
FILES = ['loadfueltype.csv', 'loadareas.csv', 'genloadareaperc.csv']


class RelaxedGovTwAdapter(HTTPAdapter):
    """只放寬 X509 strict，其餘驗證全部保留。"""

    def init_poolmanager(self, connections, maxsize, block=False, **kw):
        ctx = ssl.create_default_context()
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        # 防止日後被偷改成 CERT_NONE
        assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
        kw['ssl_context'] = ctx
        self.poolmanager = PoolManager(
            num_pools=connections, maxsize=maxsize, block=block, **kw)


def session() -> requests.Session:
    s = requests.Session()
    s.mount('https://', RelaxedGovTwAdapter())
    s.headers.update({'User-Agent': UA})
    return s


def preflight() -> int:
    s = session()
    blocked = tls_issue = False
    for f in FILES:
        try:
            r = s.get(f'{BASE}/{f}', timeout=25)
        except requests.exceptions.SSLError as exc:
            print(f'  ✕ {f}: TLS 失敗 — {str(exc)[:90]}')
            tls_issue = True
            continue
        except Exception as exc:
            print(f'  ✕ {f}: {type(exc).__name__} — {str(exc)[:80]}')
            blocked = True
            continue
        # ★ 只看狀態碼會被騙：官網回 200 但內容是 CloudFront 403 HTML 是踩過的坑
        if r.status_code != 200 or b'<html' in r.content[:400].lower():
            print(f'  ✕ {f}: HTTP {r.status_code}'
                  f'{"（內容是 HTML 不是 CSV）" if r.status_code == 200 else ""}')
            blocked = True
        else:
            rows = len(r.content.splitlines())
            print(f'  ✓ {f}: HTTP 200，{len(r.content)} bytes，{rows} 列')
    if tls_issue:
        print('\n→ 這是 TLS 問題不是封鎖：確認 RelaxedGovTwAdapter 有生效。'
              '\n  換網路沒有用。（curl 測起來會是好的，因為 curl 沒有這項檢查。）')
        return 2
    if blocked:
        print('\n→ 這台機器的出口 IP 被 CloudFront 擋了。換機器或換網路——'
              '\n  這個專案的全部前提就是這一項。')
        return 1
    print('\n→ 前置檢查通過，可以往下做（見 CLAUDE.md 的欄位對應與紀律）')
    return 0


if __name__ == '__main__':
    print('前置檢查：這台機器能不能抓台電官網\n')
    sys.exit(preflight())

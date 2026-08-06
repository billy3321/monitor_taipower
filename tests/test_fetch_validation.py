"""抓取的內容驗證——★ 空回應／挑戰頁不等於「今天沒資料」，一律當失敗。"""
import pytest
import requests

from taipower_curve import fetch


def response(content: bytes, ctype: str = 'text/csv', status: int = 200):
    r = requests.Response()
    r.status_code = status
    r._content = content
    r.headers['content-type'] = ctype
    return r


# ── 挑戰頁／擋頁 ────────────────────────────────────────────────

def test_html_body_with_csv_content_type_is_failure():
    """★ 只看 content-type 會被騙：擋頁有時仍標成 text/csv。"""
    with pytest.raises(fetch.FetchError, match='HTML'):
        fetch._validate('loadareas.csv', response(b'<html><body>403</body></html>'))


def test_html_content_type_is_failure():
    """★ 只看內容也會漏：content-type 說是 HTML 就別再猜了。"""
    with pytest.raises(fetch.FetchError, match='HTML'):
        fetch._validate('loadareas.csv', response(b'blocked', 'text/html'))


def test_doctype_prefix_is_failure():
    with pytest.raises(fetch.FetchError, match='HTML'):
        fetch._validate('loadareas.csv',
                        response(b'<!DOCTYPE html>\n<html>', 'text/plain'))


# ── 空回應 ──────────────────────────────────────────────────────

def test_empty_body_is_failure_not_no_data():
    with pytest.raises(fetch.FetchError, match='不等於今天沒資料'):
        fetch._validate('loadareas.csv', response(b''))


def test_whitespace_only_body_is_failure():
    with pytest.raises(fetch.FetchError, match='不等於今天沒資料'):
        fetch._validate('loadareas.csv', response(b'   \n\n  '))


# ── JSON 的形態 ────────────────────────────────────────────────

def test_valid_json_passes():
    fetch._validate('loadpara.json',
                    response(b'{"records": []}', 'application/json'))


def test_json_endpoint_returning_csv_is_failure():
    with pytest.raises(fetch.FetchError, match='期望 JSON'):
        fetch._validate('loadpara.json', response(b'00,1.0,2.0', 'text/csv'))


def test_json_content_type_but_unparseable_is_failure():
    with pytest.raises(fetch.FetchError, match='解不開'):
        fetch._validate('loadpara.json',
                        response(b'{"records": [', 'application/json'))


def test_csv_endpoint_returning_json_is_failure():
    with pytest.raises(fetch.FetchError, match='期望 CSV'):
        fetch._validate('loadareas.csv', response(b'{"a": 1}', 'application/json'))


# ── 正常情況 ────────────────────────────────────────────────────

def test_normal_csv_passes():
    fetch._validate('loadareas.csv', response(b'00,47.0,1114.9,973.2,1132.1\n'))


def test_bom_prefixed_json_passes():
    fetch._validate('loadpara.json',
                    response('﻿{"records": []}'.encode(), 'application/json'))


# ── 每支檔都要有 Referer 與來源網址 ──────────────────────────────

def test_every_file_has_referer_and_expectation():
    for name in fetch.FILES:
        assert name in fetch.REFERERS, f'{name} 沒有對應的 Referer'
        assert name in fetch.EXPECTED, f'{name} 沒有宣告期望的內容形態'


def test_source_url_is_full_url():
    url = fetch.source_url('loadpara.json')
    assert url.startswith('https://www.taipower.com.tw/')
    assert url.endswith('/loadpara.json')

"""原文歸檔的測試——★ 平台紀律：原文全存，解析錯了可以重跑。"""
from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from taipower_curve import archive

TAIPEI = timezone(timedelta(hours=8))
WHEN = datetime(2026, 8, 6, 14, 55, 2, tzinfo=TAIPEI)
BODIES = {
    'loadareas.csv': b'00,47.0,1114.9,973.2,1132.1\n',
    'loadpara.json': b'{"records": [{"curr_load": "4058.2"}]}',
}


@pytest.fixture
def tmp_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, 'RAW_DIR', tmp_path / 'raw')
    return tmp_path / 'raw'


def url_of(name):
    return f'https://www.taipower.com.tw/d006/loadGraph/loadGraph/data/{name}'


def test_writes_raw_bytes_unchanged(tmp_archive):
    run_dir, _ = archive.archive_run(BODIES, url_of, fetched_at=WHEN)
    for name, body in BODIES.items():
        assert (run_dir / name).read_bytes() == body, '原文必須逐 byte 相同'


def test_manifest_records_url_size_and_hash(tmp_archive):
    run_dir, manifest_sha = archive.archive_run(BODIES, url_of, fetched_at=WHEN)
    manifest = (run_dir / 'MANIFEST.txt').read_bytes()
    assert hashlib.sha256(manifest).hexdigest() == manifest_sha
    text = manifest.decode()
    for name, body in BODIES.items():
        assert url_of(name) in text, '★ 抓下來的資料要帶原始網址'
        assert str(len(body)) in text
        assert hashlib.sha256(body).hexdigest() in text


def test_archive_is_reparseable(tmp_archive):
    """歸檔的意義就在這裡：解析改壞了可以拿原文重跑。"""
    from taipower_curve import parser as P
    run_dir, _ = archive.archive_run(BODIES, url_of, fetched_at=WHEN)
    body = (run_dir / 'loadareas.csv').read_bytes()
    pts = P.parse_curve(body, 'area', P.AREA_COLUMNS, WHEN.date())
    assert {p.label: p.mw for p in pts}['北部'] == 11321.0


def test_nothing_fetched_writes_nothing(tmp_archive):
    assert archive.archive_run({}, url_of, fetched_at=WHEN) == (None, None)
    assert not tmp_archive.exists()


def test_partial_fetch_archives_what_it_got(tmp_archive):
    """抓失敗沒有原文可存，但成功的那幾支還是要留下來。"""
    run_dir, _ = archive.archive_run({'loadpara.json': BODIES['loadpara.json']},
                                     url_of, fetched_at=WHEN)
    assert (run_dir / 'loadpara.json').exists()
    assert not (run_dir / 'loadareas.csv').exists()


def test_runs_do_not_overwrite_each_other(tmp_archive):
    a, _ = archive.archive_run(BODIES, url_of, fetched_at=WHEN)
    b, _ = archive.archive_run(BODIES, url_of,
                               fetched_at=WHEN + timedelta(hours=1))
    assert a != b


# ── 保留期限 ────────────────────────────────────────────────────

def test_prune_removes_only_old_day_dirs(tmp_archive):
    tmp_archive.mkdir(parents=True)
    today = datetime.now().date()
    old = (today - timedelta(days=200)).isoformat()
    recent = (today - timedelta(days=3)).isoformat()
    for d in (old, recent):
        (tmp_archive / d / '120000').mkdir(parents=True)
    assert archive.prune(retention_days=90) == 1
    assert not (tmp_archive / old).exists()
    assert (tmp_archive / recent).exists(), '保留期內的不可以刪'


def test_prune_ignores_non_date_directories(tmp_archive):
    """★ 這個函式會 rmtree，寧可漏刪也不要刪錯。"""
    tmp_archive.mkdir(parents=True)
    (tmp_archive / 'important-do-not-delete').mkdir()
    assert archive.prune(retention_days=0) == 0
    assert (tmp_archive / 'important-do-not-delete').exists()


def test_prune_on_missing_dir_is_noop(tmp_archive):
    assert archive.prune() == 0

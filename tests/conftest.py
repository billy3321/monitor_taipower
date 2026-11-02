import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolated_spool(tmp_path, monkeypatch):
    """★ 每條測試都用自己的空暫存目錄。

    run_once._run 一開頭就會回傳離線暫存——測試若讀到這台機器 data/spool/
    裡真的暫存，會把真的待回傳資料交給假的 DB 然後刪掉。
    """
    from taipower_curve import spool
    monkeypatch.setattr(spool, 'SPOOL_DIR', tmp_path / 'spool')

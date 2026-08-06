"""原文歸檔——★ 平台紀律：原文全存，解析錯了可以重跑。

一次執行一個目錄，裡面是**未經任何處理的原始 bytes** 加一份 MANIFEST：

    data/raw/2026-08-06/145502/
        loadfueltype.csv
        loadareas.csv
        genloadareaperc.csv
        loadpara.json
        MANIFEST.txt        ← 每支檔的來源網址、bytes、sha256

★ 歸檔要在**解析之前**做。解析失敗才是最需要原文的時候——先存下來，
  之後才有得重跑；反過來做的話，來源改版那天你會兩手空空。

★ 只存抓成功的檔。抓失敗沒有原文可存，那件事記在 fetch_run 的 status。
"""
from datetime import datetime
from pathlib import Path
import hashlib
import logging
import shutil

from .parser import TAIPEI

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / 'data' / 'raw'

# 一次執行約 11 KB（4 個檔），25 次/日 ≈ 280 KB/日 ≈ 100 MB/年。
# 留 90 天約 25 MB，對這台機器微不足道，但不設上限遲早會有人來問磁碟。
RETENTION_DAYS = 90


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def archive_run(bodies: dict[str, bytes], url_of, *,
                fetched_at: datetime) -> tuple[Path, str] | tuple[None, None]:
    """寫下這次執行的原文，回 (目錄, MANIFEST 的 sha256)。

    回傳值進 monitor_fetch_run 的 raw_uri / raw_sha256：raw_uri 指到這個目錄，
    raw_sha256 是 MANIFEST 的雜湊——MANIFEST 裡有每支檔各自的雜湊，
    所以驗一個就能驗全部。
    """
    if not bodies:
        return None, None
    # ★ 目錄名用台北時間（人要找得到），但要**明示** Asia/Taipei——
    #   不可用 astimezone() 不帶參數：那會跟著這台機器的系統時區跑，
    #   換個地區或改了系統設定，同一天的歸檔就會散在兩個日期目錄裡。
    local = fetched_at.astimezone(TAIPEI)
    run_dir = RAW_DIR / f'{local:%Y-%m-%d}' / f'{local:%H%M%S}'
    run_dir.mkdir(parents=True, exist_ok=True)

    lines = [f'# 台電原文歸檔  fetched_at={fetched_at.isoformat()}', '']
    for name in sorted(bodies):
        body = bodies[name]
        (run_dir / name).write_bytes(body)   # ★ 原始 bytes，不轉碼不重排
        lines.append(f'{name}\t{url_of(name)}\t{len(body)}\t{sha256(body)}')
    manifest = ('\n'.join(lines) + '\n').encode('utf-8')
    (run_dir / 'MANIFEST.txt').write_bytes(manifest)
    return run_dir, sha256(manifest)


def prune(retention_days: int = RETENTION_DAYS) -> int:
    """刪掉超過保留天數的日期目錄，回刪掉幾個。

    ★ 只認 YYYY-MM-DD 形狀的目錄名，不做遞迴 glob 刪除——
      這個函式會 rmtree，寧可漏刪也不要刪錯。
    """
    if not RAW_DIR.exists():
        return 0
    # 目錄名是台北日期，比對基準也要用台北日期，否則跨日前後會差一天
    cutoff = datetime.now(TAIPEI).date()
    removed = 0
    for day_dir in RAW_DIR.iterdir():
        if not day_dir.is_dir():
            continue
        try:
            day = datetime.strptime(day_dir.name, '%Y-%m-%d').date()
        except ValueError:
            continue                         # 名字不是日期就不碰
        if (cutoff - day).days > retention_days:
            shutil.rmtree(day_dir)
            removed += 1
    if removed:
        log.info('原文歸檔已清掉 %d 個超過 %d 天的日期目錄', removed, retention_days)
    return removed

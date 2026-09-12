"""讀 config/config.yml。沒有抽象層，就是一個 dict 加幾個取值函式。"""
from pathlib import Path
from typing import Any
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / 'config' / 'config.yml'
ALLOWED_DB_USERS = ('crawler', 'dashboard')


class ConfigError(Exception):
    pass


def load(path: Path | None = None) -> dict[str, Any]:
    path = path or CONFIG_PATH
    if not path.exists():
        raise ConfigError(
            f'找不到 {path}——請先 cp config/config.yml.example config/config.yml 並填入密碼')
    with path.open(encoding='utf-8') as fh:
        return yaml.safe_load(fh) or {}


def database(cfg: dict[str, Any]) -> dict[str, Any]:
    env = cfg.get('environment', 'production')
    try:
        db = cfg['database'][env]
    except KeyError:
        raise ConfigError(f'config.yml 缺 database.{env}') from None
    if db.get('password') in (None, '', 'CHANGE_ME'):
        raise ConfigError('config.yml 的資料庫密碼還沒填')
    # ★ 帳號限這兩個，其他的一律 fail-fast：兩張表的擁有者是 crawler（全部權限），
    #   dashboard 另有 INSERT/UPDATE（2026-09-12 在 strait_info_monitor_prod 實查 ACL）。
    #   拿到別的帳號，錯誤會拖到很後面的 INSERT 才出現，所以在這裡就擋。
    #   ★ 舊註解「必須用 dashboard，crawler 會 permission denied」是 2026-08-05
    #     在舊庫 dashboard_monitor 實測的，搬庫後不成立，別再抄回來。
    if db.get('user') not in ALLOWED_DB_USERS:
        raise ConfigError(
            f"資料庫帳號是 {db.get('user')!r}，只接受 {ALLOWED_DB_USERS}"
            '——crawler 是兩張表的擁有者，dashboard 另有寫入權限')
    return db


VALID_MODES = ('normal', 'backup')


def mode(cfg: dict[str, Any], *, force_backup: bool = False) -> str:
    """執行模式：normal 照常抓寫；backup 先看主端還活著沒有。

    ★ 認不得的字串要**丟例外**，不可以默默退回 normal：`mode: backupp`
      這種打錯字會讓一台以為自己在待命的機器每小時跟主端搶著抓，
      而且 log 與畫面**看起來完全正常**——這個家族最常踩的形狀。
    """
    if force_backup:
        return 'backup'
    m = cfg.get('mode') or 'normal'
    if m not in VALID_MODES:
        raise ConfigError(
            f'config.yml 的 mode={m!r} 認不得，只能是 {VALID_MODES} 其中之一')
    return m


def resolve_path(value: str) -> str:
    """config 裡的相對路徑是相對專案根，不是相對 cwd。"""
    p = Path(value)
    return str(p if p.is_absolute() else ROOT / p)

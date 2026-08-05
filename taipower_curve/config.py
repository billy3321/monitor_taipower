"""讀 config/config.yml。沒有抽象層，就是一個 dict 加幾個取值函式。"""
from pathlib import Path
from typing import Any
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / 'config' / 'config.yml'


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
    # ★ monitor_power_load_curve 的寫入權限只授予 dashboard；用 crawler 連得上
    #   但 INSERT 會 permission denied，而且錯誤發生在很後面才看得到。
    if db.get('user') != 'dashboard':
        raise ConfigError(
            f"資料庫帳號是 {db.get('user')!r}，但 monitor_power_load_curve 的寫入權限"
            '只授予 dashboard——用別的帳號會 permission denied')
    return db


def resolve_path(value: str) -> str:
    """config 裡的相對路徑是相對專案根，不是相對 cwd。"""
    p = Path(value)
    return str(p if p.is_absolute() else ROOT / p)

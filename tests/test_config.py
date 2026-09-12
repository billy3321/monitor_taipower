"""config.database() 的帳號檢查：只接受 crawler / dashboard，其他 fail-fast。

★ 兩張表的擁有者是 crawler（2026-09-12 實查 ACL）。舊版硬性要求 dashboard，
  那是舊庫 dashboard_monitor 時代的事。
"""
import pytest

from taipower_curve import config as cfgmod


def _cfg(user):
    return {'database': {'production': {'user': user, 'password': 'x'}}}


@pytest.mark.parametrize('user', ['crawler', 'dashboard'])
def test_accepts_both_known_accounts(user):
    assert cfgmod.database(_cfg(user))['user'] == user


def test_rejects_unknown_account():
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.database(_cfg('analyst'))


def test_rejects_unfilled_password():
    cfg = _cfg('crawler')
    cfg['database']['production']['password'] = 'CHANGE_ME'
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.database(cfg)

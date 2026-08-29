"""跨執行的一點點本機狀態。目前只有一件事：欄位對應**上一次驗證成功**是什麼時候。

★★ 為什麼需要它：欄位驗證抓不到 unitdata.json 時會退回「按寫死的欄序解析」。
   那對單次執行是對的（驗證資料壞掉不該讓曲線一起陪葬），但如果台電哪天
   把那支開放資料的網址換掉，我們就會**一路退回猜測模式**，而降級只寫在
   note 裡、沒有人在看。這正是這個家族一再踩到的形狀：
   「壞掉的東西長得跟正常的一模一樣」。

★ 記「上次成功驗證的時間」而不是「連續退回幾次」：次數會被機器關機騙過去
  （沒跑就不會累加，時間卻照走）。我們要問的是「我們有多久沒驗過了」。
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / 'data'
LAST_VERIFIED = STATE_DIR / 'last_column_verify.txt'

# 超過這麼久沒驗證成功，就不再只是 note，要讓那次執行變成失敗（告警才會燒）。
MAX_UNVERIFIED = timedelta(hours=24)


def mark_verified(now: datetime) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LAST_VERIFIED.write_text(now.astimezone(timezone.utc).isoformat(),
                             encoding='utf-8')


def unverified_for(now: datetime) -> timedelta | None:
    """距上次驗證成功多久。從來沒驗過（檔案不存在）回 None。

    ★ 回 None 的意思是「未知」不是「零」——第一次部署、或有人清掉 data/
      都會這樣。呼叫端不該把它當成「剛驗過」。
    """
    try:
        raw = LAST_VERIFIED.read_text(encoding='utf-8').strip()
    except OSError:
        return None
    try:
        return now - datetime.fromisoformat(raw)
    except ValueError:
        logger.warning('last_column_verify.txt 內容認不得：%r', raw[:60])
        return None

#!/usr/bin/env python3
"""一次性回填 kind='area_net'（各區發電−用電）——2026-09-19 新派生的 kind。

parser 從 2026-09-19.1 起每次跑都會寫 area_net，但 8/5 以來既有的
area_gen／area_load 列不會有人回頭算。這支用同一條規則（gen − load、
任一邊 NULL 就不派生、同時戳同區）從既有列補齊，可重複執行（ON CONFLICT DO NOTHING：
parser 之後寫的新列不會被舊值蓋掉）。

用法（在裝了 config.yml 的機器上）：
    ./venv/bin/python scripts/backfill_area_net.py [--dry-run]
"""
import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from taipower_curve import config as cfgmod   # noqa: E402
from taipower_curve import db                  # noqa: E402
from taipower_curve.parser import PARSER_VERSION   # noqa: E402

log = logging.getLogger('backfill_area_net')

# ★ 與 parser.parse_areaperc 同一條規則：gen − load、四捨五入到 0.1、
#   任一邊 NULL 不派生（未知≠零）。JOIN 同時戳同區。
_SQL = text("""
INSERT INTO monitor_power_load_curve (observed_at, kind, label, mw, parser_version, fetched_at)
SELECT g.observed_at, 'area_net', g.label, round((g.mw - l.mw)::numeric, 1)::float8,
       :ver, :now
FROM monitor_power_load_curve g
JOIN monitor_power_load_curve l
  ON l.observed_at = g.observed_at AND l.label = g.label AND l.kind = 'area_load'
WHERE g.kind = 'area_gen' AND g.mw IS NOT NULL AND l.mw IS NOT NULL
ON CONFLICT (observed_at, kind, label) DO NOTHING
""")

_COUNT = text("SELECT count(*) FROM monitor_power_load_curve WHERE kind = 'area_net'")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='只算不寫')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')

    engine = db.make_engine(cfgmod.load())
    with engine.begin() as conn:
        before = conn.execute(_COUNT).scalar_one()
        if args.dry_run:
            n = conn.execute(text("""
                SELECT count(*) FROM monitor_power_load_curve g
                JOIN monitor_power_load_curve l
                  ON l.observed_at = g.observed_at AND l.label = g.label AND l.kind = 'area_load'
                WHERE g.kind = 'area_gen' AND g.mw IS NOT NULL AND l.mw IS NOT NULL
            """)).scalar_one()
            log.info('dry-run：現有 area_net %d 筆，可派生的 gen/load 配對 %d 筆', before, n)
            return 0
        res = conn.execute(_SQL, {'ver': PARSER_VERSION,
                                  'now': datetime.now(timezone.utc)})
        after = conn.execute(_COUNT).scalar_one()
    log.info('area_net：回填前 %d、新增 %d、回填後 %d 筆', before, res.rowcount, after)
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""rules/indicators.md의 API 출처에서 데이터를 받아 data/cache/에 저장한다.

사용법: python scripts/fetch.py [--date YYYY-MM-DD] [--market kr]
"""
import argparse
import json
import re
import sys
import warnings
from datetime import date, datetime

import pandas as pd

import sources
import spec

CACHE = spec.ROOT / "data" / "cache"
STATUS = CACHE / "_status.json"
# 3년 차트 + 20주 이동평균 + 전년비 계산에 필요한 기간
LOOKBACK = pd.DateOffset(years=4, months=6)


def cache_key(source, ohlcv=False):
    return f"ohlcv|{source}" if ohlcv else source


def cache_path(source, ohlcv=False):
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", source)
    return CACHE / (f"ohlcv_{name}.csv" if ohlcv else f"{name}.csv")


def api_sources(source):
    """지표 출처에서 API로 받아야 하는 출처를 뽑는다. calc 식 안의 {출처}도 포함한다."""
    if source.startswith("calc:"):
        return [r for r in re.findall(r"\{([^}]+)\}", source) if r.split(":")[0] in sources.API_KINDS]
    return [source] if source.split(":")[0] in sources.API_KINDS else []


def jobs_for(markets):
    jobs = {}
    for m in markets:
        for idx in m.indices:
            jobs[(idx.source, True)] = None
            if idx.volume != "= 지수":
                jobs[(idx.volume, True)] = None
        for axis in m.axes:
            for ind in axis.indicators:
                for src in api_sources(ind.source):
                    jobs[(src, False)] = None
    return list(jobs)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=f"{date.today():%Y-%m-%d}")
    p.add_argument("--market")
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    warnings.filterwarnings("ignore")

    markets = [m for m in spec.load() if not args.market or m.code == args.market]
    start = pd.Timestamp(args.date) - LOOKBACK
    CACHE.mkdir(parents=True, exist_ok=True)
    status = json.loads(STATUS.read_text(encoding="utf-8")) if STATUS.exists() else {}

    jobs = jobs_for(markets)
    print(f"{len(jobs)}개 출처 수집 (시작일 {start:%Y-%m-%d})")
    for src, ohlcv in jobs:
        key = cache_key(src, ohlcv)
        now = datetime.now().isoformat(timespec="seconds")
        try:
            data = sources.fetch(src, start, ohlcv)
            if len(data) == 0:
                raise ValueError("빈 데이터")
            data.to_csv(cache_path(src, ohlcv))
            last = f"{data.index.max():%Y-%m-%d}"
            status[key] = {"ok": True, "rows": len(data), "last": last, "fetched": now}
            print(f"  OK   {key}  ({len(data)}행, 최신 {last})")
        except Exception as e:
            status[key] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300], "fetched": now}
            print(f"  FAIL {key}  {status[key]['error']}")
        STATUS.write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")

    failed = [k for k, v in status.items() if not v["ok"]]
    print(f"완료. 실패 {len(failed)}건" + (f": {', '.join(failed)}" if failed else ""))


if __name__ == "__main__":
    main()

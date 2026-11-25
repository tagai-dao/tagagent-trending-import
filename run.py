"""Run one locked import cycle."""
import json
from pathlib import Path

import portalocker

from tagagent.agents.trader.trending_import import run


def main():
    path = Path(__file__).resolve().parent / "runtime" / "locks" / "trending-import.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.exceptions.AlreadyLocked:
            print('{"status":"skipped","reason":"trending import already running"}')
            return 0
        result = run()
        print(json.dumps(result, default=str, ensure_ascii=True))
        return int(result.get("status") == "partial_failure")


if __name__ == "__main__":
    raise SystemExit(main())

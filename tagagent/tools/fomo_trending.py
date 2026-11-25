"""Read fresh FOMO Trending snapshots produced by the browser feed worker."""

from __future__ import annotations

from datetime import datetime, timezone

from tagagent import config
from tagagent.wiki import load_json


class FOMOTrendingError(RuntimeError):
    pass


def trending(chain: str, *, limit: int = 50) -> list[dict]:
    payload = load_json(config.TRENDING_IMPORT_FOMO_SNAPSHOT, {})
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        raise FOMOTrendingError("FOMO Trending snapshot is missing or invalid")
    try:
        scanned_at = datetime.fromisoformat(str(payload["scannedAt"]).replace("Z", "+00:00"))
        if scanned_at.tzinfo is None:
            scanned_at = scanned_at.replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError) as exc:
        raise FOMOTrendingError("FOMO Trending snapshot has an invalid timestamp") from exc
    age = (datetime.now(timezone.utc) - scanned_at).total_seconds()
    if age < -60 or age > config.TRENDING_IMPORT_FOMO_SNAPSHOT_MAX_AGE_SECONDS:
        raise FOMOTrendingError(f"FOMO Trending snapshot is stale (age={int(age)}s)")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise FOMOTrendingError("FOMO Trending snapshot candidates are invalid")
    rows = [
        row for row in candidates
        if isinstance(row, dict) and str(row.get("chain") or "").lower() == chain
    ]
    if not rows:
        raise FOMOTrendingError(f"FOMO Trending snapshot has no {chain} candidates")
    return rows[:max(1, int(limit))]

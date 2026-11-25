"""Small read-only client for GMGN market rankings."""

from __future__ import annotations

import time
import uuid
import json
import os

import requests

from tagagent import config


class GMGNError(RuntimeError):
    pass


RATE_LIMIT_PATH = config.RUNTIME_ROOT / "gmgn-rate-limit.json"
MIN_REQUEST_INTERVAL_SECONDS = 1.1
_last_request_at = 0.0
_prefer_environment_proxy = False


def _connection_options(use_proxy: bool | None = None) -> dict:
    # An explicit empty HTTPS proxy overrides requests' HTTPS_PROXY and
    # ALL_PROXY environment settings without changing other HTTP clients.
    if use_proxy is None:
        use_proxy = config.GMGN_API_USE_PROXY
    return {} if use_proxy else {"proxies": {"https": ""}}


def _proxy_failover(operation):
    if config.GMGN_API_USE_PROXY:
        return None
    if not requests.utils.get_environ_proxies(config.GMGN_API_BASE).get("https"):
        return None
    return lambda base: operation(base, True)


def _check_cooldown() -> None:
    try:
        until = float(json.loads(RATE_LIMIT_PATH.read_text()).get("reset_at", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return
    if until > time.time():
        raise GMGNError(f"GMGN rate-limit cooldown until {until:.0f}; no request sent")


def _record_rate_limit(response) -> float:
    now = time.time()
    until = now + 60
    try:
        until = max(until, float(response.json().get("reset_at", 0)) + 1)
    except (ValueError, TypeError, AttributeError):
        pass
    try:
        until = max(until, now + float(response.headers.get("Retry-After", 0)))
    except (ValueError, TypeError, AttributeError):
        pass
    RATE_LIMIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = RATE_LIMIT_PATH.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"reset_at": until, "observed_at": now}))
    os.replace(tmp, RATE_LIMIT_PATH)
    return until


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 429 or (isinstance(status, int) and status >= 500)


def _request_json(operation, failure_label: str, *, proxy_operation=None) -> object:
    """Run one idempotent GMGN read with retries and endpoint failover."""
    global _last_request_at, _prefer_environment_proxy
    _check_cooldown()
    attempts = config.GMGN_API_MAX_ATTEMPTS
    bases = tuple(dict.fromkeys((config.GMGN_API_BASE, *config.GMGN_API_FALLBACK_BASES)))
    failures: list[str] = []
    last_error: Exception | None = None
    for base in bases:
        for attempt in range(attempts):
            try:
                delay = MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
                if delay > 0:
                    time.sleep(delay)
                _last_request_at = time.monotonic()
                if proxy_operation is None:
                    response = operation(base)
                    response.raise_for_status()
                else:
                    primary, secondary = (
                        (proxy_operation, operation)
                        if _prefer_environment_proxy else (operation, proxy_operation)
                    )
                    try:
                        response = primary(base)
                        response.raise_for_status()
                    except (requests.ConnectionError, requests.Timeout):
                        # A transport failure can be route-specific.  A 429 or
                        # other HTTP response must never trigger a second route.
                        response = secondary(base)
                        response.raise_for_status()
                        _prefer_environment_proxy = secondary is proxy_operation
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                response = getattr(exc, "response", None)
                if getattr(response, "status_code", None) == 429:
                    until = _record_rate_limit(response)
                    raise GMGNError(f"{failure_label}: rate limited; cooldown until {until:.0f}") from exc
                retryable = _is_retryable(exc)
                if not retryable:
                    raise GMGNError(f"{failure_label}: {exc}") from exc
                if attempt + 1 < attempts:
                    time.sleep(config.GMGN_API_RETRY_BACKOFF_SECONDS * (2 ** attempt))
        failures.append(f"{base}: {last_error}")
    detail = "; ".join(failures)
    raise GMGNError(f"{failure_label}: all endpoints failed ({detail})") from last_error


def trending(chain: str, *, limit: int = 50, interval: str = "5m") -> object:
    if not config.GMGN_API_KEY:
        raise GMGNError("GMGN_API_KEY is not configured")
    def operation(base, use_proxy: bool):
        # Regenerate these on every attempt because GMGN validates Unix seconds
        # within a narrow clock-skew window and uses client_id per request.
        params = {
            "chain": chain,
            "interval": interval,
            "limit": min(100, max(1, int(limit))),
            "timestamp": int(time.time()),
            "client_id": str(uuid.uuid4()),
        }
        return requests.get(
            f"{base}/v1/market/rank",
            params=params,
            headers={"X-APIKEY": config.GMGN_API_KEY, "User-Agent": "TagAgentX/1.0"},
            timeout=30,
            **_connection_options(use_proxy),
        )
    payload = _request_json(
        lambda base: operation(base, config.GMGN_API_USE_PROXY),
        "GMGN market rank request failed",
        proxy_operation=_proxy_failover(operation),
    )
    if isinstance(payload, dict) and payload.get("code") not in (None, 0, 200, "0", "200"):
        raise GMGNError(str(payload.get("message") or payload.get("error") or "GMGN API error"))
    return payload


def hot_searches(
    chains: tuple[str, ...],
    intervals: tuple[str, ...],
    *,
    limit: int = 50,
) -> object:
    """Return GMGN's hot-search rankings for every requested chain/window."""
    if not config.GMGN_API_KEY:
        raise GMGNError("GMGN_API_KEY is not configured")
    params = [
        {
            "label": "hot-search", "chain": chain, "interval": interval,
            "limit": min(500, max(1, int(limit))),
        }
        for chain in chains
        for interval in intervals
    ]
    def operation(base, use_proxy: bool):
        query = {"timestamp": int(time.time()), "client_id": str(uuid.uuid4())}
        return requests.post(
            f"{base}/v1/market/hot_searches",
            params=query,
            json={"params": params},
            headers={
                "X-APIKEY": config.GMGN_API_KEY,
                "Content-Type": "application/json",
                "User-Agent": "TagAgentX/1.0",
            },
            timeout=30,
            **_connection_options(use_proxy),
        )
    payload = _request_json(
        lambda base: operation(base, config.GMGN_API_USE_PROXY),
        "GMGN hot-search request failed",
        proxy_operation=_proxy_failover(operation),
    )
    if isinstance(payload, dict) and payload.get("code") not in (None, 0, 200, "0", "200"):
        raise GMGNError(str(payload.get("message") or payload.get("error") or "GMGN API error"))
    return payload


def token_pool(chain: str, address: str) -> object:
    """Return GMGN's primary liquidity-pool record for a token."""
    if not config.GMGN_API_KEY:
        raise GMGNError("GMGN_API_KEY is not configured")

    def operation(base, use_proxy: bool):
        params = {
            "chain": chain,
            "address": address,
            "timestamp": int(time.time()),
            "client_id": str(uuid.uuid4()),
        }
        return requests.get(
            f"{base}/v1/token/pool_info",
            params=params,
            headers={"X-APIKEY": config.GMGN_API_KEY, "User-Agent": "TagAgentX/1.0"},
            timeout=30,
            **_connection_options(use_proxy),
        )

    payload = _request_json(
        lambda base: operation(base, config.GMGN_API_USE_PROXY),
        "GMGN token pool request failed",
        proxy_operation=_proxy_failover(operation),
    )
    if isinstance(payload, dict) and payload.get("code") not in (None, 0, 200, "0", "200"):
        raise GMGNError(str(payload.get("message") or payload.get("error") or "GMGN API error"))
    return payload


def token_call_signals(
    chain: str,
    *,
    min_market_cap_usd: int = 300_000,
    since_timestamp: int | None = None,
) -> object:
    """Return GMGN platform/KOL call signals for exact-CA filtering by caller."""
    if not config.GMGN_API_KEY:
        raise GMGNError("GMGN_API_KEY is not configured")
    groups: list[dict] = [
        {"signal_type": [13, 19, 20], "mc_min": int(min_market_cap_usd)},
    ]
    if since_timestamp:
        groups[0]["min_create_or_open_ts"] = str(int(since_timestamp))
    def operation(base, use_proxy: bool):
        params = {"timestamp": int(time.time()), "client_id": str(uuid.uuid4())}
        return requests.post(
            f"{base}/v1/market/token_signal",
            params=params,
            json={"chain": chain, "groups": groups},
            headers={
                "X-APIKEY": config.GMGN_API_KEY,
                "Content-Type": "application/json",
                "User-Agent": "TagAgentX/1.0",
            },
            timeout=30,
            **_connection_options(use_proxy),
        )
    payload = _request_json(
        lambda base: operation(base, config.GMGN_API_USE_PROXY),
        "GMGN token signal request failed",
        proxy_operation=_proxy_failover(operation),
    )
    if isinstance(payload, dict) and payload.get("code") not in (None, 0, 200, "0", "200"):
        raise GMGNError(str(payload.get("message") or payload.get("error") or "GMGN API error"))
    return payload

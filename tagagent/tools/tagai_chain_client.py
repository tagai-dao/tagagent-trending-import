"""Chain-scoped TagAI community API helpers."""

from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation

import requests

from tagagent import config


class TagAIChainError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code or message


def _request(method: str, path: str, chain_id: int, **kwargs):
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "TagAgentX/1.0",
        "X-Chain-Id": str(chain_id),
    }
    attempts = 3 if method.upper() == "GET" else 1
    for attempt in range(attempts):
        try:
            response = requests.request(
                method,
                f"{config.TRENDING_IMPORT_API_BASE}{path}",
                headers=headers,
                timeout=30,
                **kwargs,
            )
            if response.status_code < 500 or attempt + 1 >= attempts:
                break
        except requests.RequestException as exc:
            if attempt + 1 >= attempts:
                raise TagAIChainError(f"TagAI request failed: {exc}") from exc
        time.sleep(0.5 * (attempt + 1))
    body = response.text[:500]
    # TagAI uses 3xx application errors (for example 301 when a selected DEX
    # pool is not in its index). requests does not raise for these responses,
    # and treating them as success can permanently poison the import ledger.
    if response.status_code >= 300:
        lowered = body.lower()
        if "tick_has_exists" in lowered or "tick has exists" in lowered:
            raise TagAIChainError("ticker_exists", code="ticker_exists")
        if "token exists" in lowered:
            raise TagAIChainError("token_exists", code="token_exists")
        # The API occasionally surfaces its own upstream RPC transport issue
        # as a 301 application response.  Unlike a missing/invalid pool this
        # is not a deterministic rejection, so permit the idempotent caller
        # to read back and retry rather than poisoning recovery state.
        if "import_validation_unavailable" in lowered or "import_validation_timeout" in lowered or (
            "could not coalesce error" in lowered
        ) or (
            "pool quote/trade validation failed" in lowered
            and ("timeout" in lowered or "timed out" in lowered)
        ):
            raise TagAIChainError(
                f"TagAI transient pool validation error: {body}",
                code="transient_pool_validation",
            )
        raise TagAIChainError(f"TagAI HTTP {response.status_code}: {body}")
    if not body:
        return None
    try:
        return response.json()
    except ValueError:
        return None


def ticker_used(tick: str, chain_id: int) -> bool:
    if tick == ".ai":
        # The native-issuance availability endpoint rejects dots before lookup.
        # Imported .ai keeps its ERC20 symbol. Check exact detail on BOTH chains
        # because community ticks are globally unique; any read error fails closed.
        for lookup_chain in (56, 4663):
            detail = community_detail(tick, lookup_chain)
            if not isinstance(detail, dict) or (detail and str(detail.get("tick", "")).lower() != tick):
                raise TagAIChainError("invalid exact ticker lookup result")
            if detail:
                return True
        return False
    return bool(_request("GET", "/community/isTokenExist", chain_id, params={"tick": tick}))


def community_detail(tick: str, chain_id: int) -> dict:
    return _request(
        "GET", "/community/detail", chain_id,
        params={"tick": tick, "_": int(time.time() * 1000)},
    ) or {}


def native_price_usd(chain_id: int) -> Decimal:
    value = _request("GET", "/tiptag/getETHPrice", chain_id)
    try:
        price = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TagAIChainError(f"invalid native asset price: {value!r}") from exc
    if price <= 0:
        raise TagAIChainError(f"invalid native asset price: {value!r}")
    return price


def validate_import_pool(token: str, pair: str, dex_version: int, creator: str, chain_id: int) -> None:
    """Require the registration service's read-only pool validation before creation."""
    result = _request("POST", "/community/validateImportPool", chain_id, json={
        "token": token, "pair": pair, "dexVersion": dex_version, "ethAddr": creator,
    })
    if not isinstance(result, dict) or result.get("valid") is not True:
        detail = result.get("error", "missing valid=true") if isinstance(result, dict) else "missing validation result"
        raise TagAIChainError(f"TagAI pool preflight rejected: {detail}", code="invalid_import_pool")


def register_import(import_info: dict, creator: str, chain_id: int) -> None:
    expected_token = str(import_info.get("token") or "").lower()
    expected_community = str(import_info.get("communityAddress") or "").lower()
    last_error = None
    for post_attempt in range(2):
        try:
            _request(
                "POST",
                "/community/importCommunity",
                chain_id,
                json={
                    "importInfo": import_info, "ethAddr": creator,
                    "signature": "", "infoStr": "",
                },
            )
            last_error = None
        except TagAIChainError as exc:
            last_error = exc
            # Application rejections are deterministic. A transport timeout
            # is ambiguous, so read back before an idempotent retry.
            if (
                "TagAI request failed" not in str(exc)
                and exc.code not in {
                    "token_exists", "ticker_exists", "transient_pool_validation",
                }
            ):
                raise
        for attempt in range(3):
            try:
                detail = community_detail(import_info["tick"], chain_id)
            except TagAIChainError as exc:
                last_error = exc
                detail = {}
            if (
                str(detail.get("token") or "").lower() == expected_token
                and str(detail.get("communityAddress") or "").lower() == expected_community
            ):
                return
            if attempt < 2:
                time.sleep(1)
        if post_attempt == 0:
            time.sleep(1)
    if last_error:
        raise last_error
    raise TagAIChainError("TagAI import response was not visible in community/detail")

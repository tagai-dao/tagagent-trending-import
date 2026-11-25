"""Import high-cap GMGN trending tokens into the matching TagAI chain."""

from __future__ import annotations

import math
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import requests
from web3.providers.base import BaseProvider

from tagagent import config
from tagagent.agents.trader.predict_market import (
    PredictStatus,
    _sign_tx,
    _signer_address,
)
from tagagent.tools import fomo_trending, gmgn_client, tagai_chain_client
from tagagent.wiki import append_jsonl, read_jsonl

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ERC20_ABI = [
    {"type": "function", "name": "symbol", "stateMutability": "view", "inputs": [], "outputs": [{"type": "string"}]},
    {"type": "function", "name": "decimals", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint8"}]},
]
COMMITTEE_ABI = [
    {"type": "function", "name": name, "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]}
    for name in ("getCreateCommunityFee", "getCommunitySettingsFee")
]
IPSHARE_ABI = [
    {"type": "function", "name": "ipshareCreated", "stateMutability": "view", "inputs": [{"type": "address"}], "outputs": [{"type": "bool"}]},
    {"type": "function", "name": "createFee", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
]
IMPORT_HELPER_ABI = [
    *[
        {"type": "function", "name": name, "stateMutability": "view", "inputs": [], "outputs": [{"type": output}]}
        for name, output in (("VERSION", "uint256"), ("defaultPoolCount", "uint256"), ("swapWrapper", "address"))
    ],
    {"type": "function", "name": "createCommunityAndPool", "stateMutability": "payable", "inputs": [{"name": "token", "type": "address"}, {"name": "existingCommunity", "type": "address"}], "outputs": [{"type": "address"}, {"type": "address"}]},
    {"type": "function", "name": "createCommunityAndPool", "stateMutability": "payable", "inputs": [{"name": "token", "type": "address"}, {"name": "calculator", "type": "address"}, {"name": "calculatorData", "type": "bytes"}], "outputs": [{"type": "address"}, {"type": "address"}]},
    {"type": "function", "name": "importerOf", "stateMutability": "view", "inputs": [{"name": "token", "type": "address"}], "outputs": [{"type": "address"}]},
    {"type": "function", "name": "agentSwapWrapper", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"type": "event", "name": "CommunityCreated", "anonymous": False, "inputs": [
        {"indexed": True, "name": "token", "type": "address"},
        {"indexed": True, "name": "community", "type": "address"},
        {"indexed": True, "name": "pool", "type": "address"},
        {"indexed": False, "name": "creator", "type": "address"},
        {"indexed": False, "name": "calculator", "type": "address"},
    ]},
]
IMPORTED_SWAP_WRAPPER_ABI = [
    {"type": "function", "name": "registrar", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"type": "function", "name": "operator", "stateMutability": "view", "inputs": [], "outputs": [{"type": "address"}]},
    {"type": "function", "name": "getImportedMarket", "stateMutability": "view", "inputs": [{"name": "token", "type": "address"}], "outputs": [
        {"name": "registered", "type": "bool"}, {"name": "community", "type": "address"}, {"name": "deployer", "type": "address"},
    ]},
    {"type": "function", "name": "quoteBuy", "stateMutability": "view", "inputs": [
        {"name": "token", "type": "address"}, {"name": "sourceType", "type": "uint8"},
        {"name": "sourceData", "type": "bytes"}, {"name": "nativeAmountIn", "type": "uint256"},
    ], "outputs": [{"name": "tokenAmountOut", "type": "uint256"}]},
    {"type": "function", "name": "quoteSell", "stateMutability": "view", "inputs": [
        {"name": "token", "type": "address"}, {"name": "sourceType", "type": "uint8"},
        {"name": "sourceData", "type": "bytes"}, {"name": "tokenAmountIn", "type": "uint256"},
    ], "outputs": [{"name": "nativeAmountOut", "type": "uint256"}]},
    {"type": "function", "name": "buyToken", "stateMutability": "payable", "inputs": [
        {"name": "token", "type": "address"}, {"name": "sourceType", "type": "uint8"},
        {"name": "sourceData", "type": "bytes"}, {"name": "minimumTokenOut", "type": "uint256"},
        {"name": "recipient", "type": "address"}, {"name": "deadline", "type": "uint256"},
        {"name": "sellsman", "type": "address"},
    ], "outputs": [{"name": "tokenOut", "type": "uint256"}]},
]
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
POOL_ID_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
TICK_RE = re.compile(r"^(?!\d+$)[A-Za-z0-9\u4e00-\u9fa5_]{1,16}$")
RANK_INTERVALS = ("1m", "5m", "1h", "6h", "24h")
POOL_SELECTION_STRATEGY_VERSION = 6
IMPORT_GAS_FALLBACKS = {
    # Both RPCs have returned an empty revert from eth_estimateGas after the
    # identical eth_call preflight succeeded.  Successful imports currently
    # consume about 730k gas, so 1.2m is a conservative, bounded fallback.
    "bsc": 1_200_000,
    "robinhood": 1_200_000,
}
TERMINAL_SKIP_REASONS = {
    "already_imported", "api_token_conflict", "invalid_tick", "ticker_used",
    "unsupported_decimals",
}


class TrendingImportError(RuntimeError):
    pass


def _is_transient_rpc_error(value: object) -> bool:
    """Return whether an RPC failure is safe to retry on another endpoint."""
    text = str(value).lower()
    return any(fragment in text for fragment in (
        "connection aborted",
        "connection reset",
        "connection refused",
        "connectionpool",
        "context deadline exceeded",
        "eof occurred",
        "gateway timeout",
        "max retries exceeded",
        "rate limit",
        "remote disconnected",
        "server disconnected",
        "service unavailable",
        "sslerror",
        "temporarily unavailable",
        "too many requests",
        "unexpected_eof",
        "upstream timeout",
    ))


def _is_empty_execution_revert(value: object) -> bool:
    """Return whether an RPC error contains no usable EVM revert data."""
    lowered = str(value).lower().strip()
    return "execution reverted" in lowered and (
        "'0x'" in lowered or '"0x"' in lowered or lowered.endswith("0x")
    )


def _is_transient_rpc_response(response: object) -> bool:
    if not isinstance(response, dict) or not isinstance(response.get("error"), dict):
        return False
    error = response["error"]
    if error.get("code") in {429, -32005}:
        return True
    return _is_transient_rpc_error(error.get("message") or error)


class _FallbackHTTPProvider(BaseProvider):
    """Build a Web3 provider that sticks to the last healthy RPC endpoint."""

    def __init__(self, urls: list[str], *, timeout: float = 30, providers=None):
        super().__init__()
        if providers is None:
            from web3 import HTTPProvider
            from web3.providers.rpc.utils import ExceptionRetryConfiguration

            retry_config = ExceptionRetryConfiguration(
                errors=(requests.ConnectionError, requests.HTTPError, requests.Timeout),
                retries=config.TRENDING_IMPORT_RPC_RETRIES_PER_ENDPOINT,
                backoff_factor=config.TRENDING_IMPORT_RPC_RETRY_BACKOFF_SECONDS,
            )
            providers = [
                HTTPProvider(
                    url,
                    request_kwargs={"timeout": timeout},
                    exception_retry_configuration=retry_config,
                )
                for url in urls
            ]
        if not providers:
            raise ValueError("at least one RPC endpoint is required")
        self.providers = list(providers)
        self.active_index = 0

    @property
    def endpoint_uri(self):
        return self.providers[self.active_index].endpoint_uri

    def make_request(self, method, params):
        last_response = None
        last_error = None
        order = [
            (self.active_index + offset) % len(self.providers)
            for offset in range(len(self.providers))
        ]
        for index in order:
            try:
                response = self.providers[index].make_request(method, params)
            except (requests.RequestException, OSError) as exc:
                last_error = exc
                continue
            last_response = response
            if _is_transient_rpc_response(response) or (
                method == "eth_estimateGas" and _is_empty_execution_revert(response)
            ):
                # Some BSC nodes return an empty revert for a valid import gas
                # estimate. Try a different implementation before falling back
                # to the bounded chain gas cap. Do not fail over informative
                # contract reverts, which must remain hard failures.
                continue
            self.active_index = index
            return response
        if last_response is not None:
            return last_response
        if last_error is not None:
            raise last_error
        raise RuntimeError("no RPC endpoints configured")

    def is_connected(self, show_traceback=False):
        try:
            response = self.make_request("web3_clientVersion", [])
            return isinstance(response, dict) and "result" in response
        except Exception:
            if show_traceback:
                raise
            return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _number(value) -> Decimal:
    try:
        return Decimal(str(value).replace(",", "").replace("$", ""))
    except (InvalidOperation, AttributeError):
        return Decimal(0)


def _first(item: dict, names: tuple[str, ...]):
    for name in names:
        value = item.get(name)
        if value not in (None, ""):
            return value
    return None


def _candidate_lists(value) -> list[list[dict]]:
    found: list[list[dict]] = []
    if isinstance(value, list):
        if value and all(isinstance(row, dict) for row in value):
            found.append(value)
        for row in value:
            found.extend(_candidate_lists(row))
    elif isinstance(value, dict):
        for child in value.values():
            found.extend(_candidate_lists(child))
    return found


def normalize_rank(payload: object, limit: int = 50) -> list[dict]:
    """Normalize minor GMGN envelope/field changes without weakening filters."""
    lists = _candidate_lists(payload)
    ranked = max(lists, key=len, default=[])
    output = []
    seen = set()
    for row in ranked:
        token = _first(row, ("address", "token_address", "tokenAddress", "contract_address", "contractAddress"))
        nested = row.get("token")
        if not ADDRESS_RE.match(str(token or "")) and isinstance(nested, dict):
            token = _first(nested, ("address", "token_address", "contract_address"))
        market_cap = _first(row, ("market_cap", "marketcap", "market_cap_usd", "usd_market_cap", "fdv", "fdv_usd"))
        if market_cap is None and isinstance(nested, dict):
            market_cap = _first(nested, ("market_cap", "marketcap", "usd_market_cap", "fdv"))
        token_text = str(token or "")
        key = token_text.lower()
        if not ADDRESS_RE.match(token_text) or key in seen:
            continue
        seen.add(key)
        output.append({
            "token": token_text,
            "market_cap_usd": _number(market_cap),
            "name": str(_first(row, ("name", "token_name")) or ""),
            "symbol": str(_first(row, ("symbol", "token_symbol")) or ""),
            "logo": str(_first(row, ("logo", "logo_url", "image_url")) or ""),
        })
        if len(output) >= limit:
            break
    return output


def _merge_rank_views(views: list[tuple[str, list[dict]]]) -> list[dict]:
    """Union rank views by contract and order candidates by their best rank."""
    merged: dict[str, dict] = {}
    order = 0
    for source, rows in views:
        for rank, row in enumerate(rows, 1):
            key = row["token"].lower()
            current = merged.get(key)
            if current is None:
                current = {**row, "best_rank": rank, "rank_sources": [], "_order": order}
                merged[key] = current
                order += 1
            current["best_rank"] = min(int(current["best_rank"]), rank)
            current["market_cap_usd"] = max(current["market_cap_usd"], row["market_cap_usd"])
            current["rank_sources"].append({"source": source, "rank": rank})
    result = sorted(merged.values(), key=lambda row: (row["best_rank"], row["_order"]))
    for row in result:
        row.pop("_order", None)
    return result


def collect_rank_candidates(chain: str, limit: int) -> tuple[list[dict], list[dict]]:
    """Collect Trending + Hot Search across all UI time windows."""
    views: list[tuple[str, list[dict]]] = []
    errors: list[dict] = []
    gmgn_success_count = 0
    for interval in RANK_INTERVALS:
        source = f"trending:{interval}"
        try:
            rows = normalize_rank(gmgn_client.trending(chain, limit=limit, interval=interval), limit)
            views.append((source, rows))
            gmgn_success_count += 1
        except Exception as exc:
            errors.append({"source": source, "error": str(exc)[:300]})
    try:
        payload = gmgn_client.hot_searches((chain,), RANK_INTERVALS, limit=limit)
        gmgn_success_count += 1
        datasets = payload.get("data", []) if isinstance(payload, dict) else []
        for interval in RANK_INTERVALS:
            dataset = next((
                item for item in datasets
                if isinstance(item, dict)
                and str(item.get("chain")) == chain
                and str(item.get("interval")) == interval
            ), {})
            views.append((
                f"hot_search:{interval}",
                normalize_rank(dataset.get("tokens", []), limit),
            ))
    except Exception as exc:
        errors.append({"source": "hot_search:all", "error": str(exc)[:300]})
    if gmgn_success_count == 0 and config.TRENDING_IMPORT_FOMO_FALLBACK_ENABLED:
        try:
            views.append(("fomo:trending", normalize_rank(fomo_trending.trending(chain, limit=limit), limit)))
        except Exception as exc:
            errors.append({"source": "fomo:trending", "error": str(exc)[:300]})
    return _merge_rank_views(views), errors


def sanitize_tick(symbol: str, *, chain: str = "", token: str = "") -> str:
    tick = re.sub(r"\s+", "_", str(symbol or "").strip())
    # Explicitly requested external Agent token. Preserve its real symbol;
    # do not relax native issuance rules or accept lookalike tokens/chains.
    if (chain == "bsc" and token.lower() == "0x7ffcf7b3b52adcc00c76f50ce2e37bf3c7e215dc"
            and tick == ".ai"):
        return tick
    return tick if TICK_RE.fullmatch(tick) else ""


def _dex_version(dex_id: str) -> int:
    value = str(dex_id).lower()
    if any(part in value for part in ("infinity-clmm", "clmm", "v4")):
        return 4
    if "v3" in value:
        return 3
    if "v2" in value or "pancakeswap" in value or "uniswap" in value:
        return 2
    return 0


def _relationship_address(pool: dict, name: str) -> str:
    value = str(pool.get("relationships", {}).get(name, {}).get("data", {}).get("id", ""))
    return value.rsplit("_", 1)[-1] if "_" in value else value


def _pool_fee_pct(chain: str, version: int, name: str) -> float:
    match = re.search(r"(?:^|\s)(\d+(?:\.\d+)?)%", str(name or ""))
    if match:
        return max(0.0, float(match.group(1)))
    if version == 2:
        return 0.25 if chain == "bsc" else 0.30
    # Missing concentrated-liquidity fee metadata is treated conservatively.
    return 1.0


def _age_score(created_at: str) -> float:
    try:
        created = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        days = max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 86400)
        return min(1.0, math.log1p(days) / math.log1p(30))
    except (TypeError, ValueError):
        return 0.0


def _relative_log_scores(values: list[float]) -> list[float]:
    logs = [math.log1p(max(0.0, value)) for value in values]
    low, high = min(logs, default=0.0), max(logs, default=0.0)
    if high <= low:
        return [1.0 for _ in logs]
    return [(value - low) / (high - low) for value in logs]


def score_pools(pools: list[dict]) -> list[dict]:
    """Score executable pools, explicitly including DEX and round-trip fees."""
    liquidity_scores = _relative_log_scores([pool["liquidity_usd"] for pool in pools])
    volume_scores = _relative_log_scores([pool["volume_24h"] for pool in pools])
    tx_scores = _relative_log_scores([pool["tx_count_24h"] for pool in pools])
    for index, pool in enumerate(pools):
        pool_fee_score = max(0.0, 1.0 - min(pool["fee_pct"], 3.0) / 3.0)
        execution_score = float(pool.get("execution_score", 0.5))
        pool["score"] = round(
            0.30 * liquidity_scores[index]
            + 0.20 * volume_scores[index]
            + 0.10 * tx_scores[index]
            + 0.10 * pool["age_score"]
            + 0.10 * pool["quote_score"]
            + 0.10 * pool_fee_score
            + 0.10 * execution_score,
            8,
        )
    return sorted(pools, key=lambda pool: (pool["score"], pool["liquidity_usd"]), reverse=True)


def _preflight_import_pool(
    w3, chain: str, token: str, pool: dict, owner: str,
) -> tuple[bool, float, str]:
    """Run tagai-ui's unified Wrapper quote/buy checks before importing."""
    from eth_abi import encode

    chain_cfg = config.TRENDING_IMPORT_CHAIN_CONFIG[chain]
    token = w3.to_checksum_address(token)
    pair = pool["pair"]
    if pool["dex_version"] == 2:
        pair = w3.to_checksum_address(pair)
        source_type = 0
        source_data = encode(["(address,address)"], [(chain_cfg["v2_router"], pair)])
    elif pool["dex_version"] == 3:
        pair = w3.to_checksum_address(pair)
        source_type = 1
        source_data = encode(
            ["(address,address,address)"],
            [(chain_cfg["v3_router"], chain_cfg["v3_quoter"], pair)],
        )
    elif (
        pool["dex_version"] == 4
        and chain == "robinhood"
        and config.TRENDING_IMPORT_ALLOW_ROBINHOOD_V4
    ):
        source_type = 2
        source_data = encode(
            ["(address,address,address,uint24,int24,address)"],
            [_resolve_robinhood_v4_pool_key(w3, pair)],
        )
    else:
        return False, 0.0, f"unsupported imported-token DEX version: {pool['dex_version']}"
    wrapper = w3.eth.contract(
        address=w3.to_checksum_address(chain_cfg["imported_swap_wrapper"]),
        abi=IMPORTED_SWAP_WRAPPER_ABI,
    )
    native_in = 10**12
    try:
        quoted_buy = int(wrapper.functions.quoteBuy(token, source_type, source_data, native_in).call())
        if quoted_buy <= 0:
            raise ValueError("buy quote returned zero")
        actual_buy = int(wrapper.functions.buyToken(
            token, source_type, source_data, 1, owner,
            int(datetime.now(timezone.utc).timestamp()) + 300, ZERO_ADDRESS,
        ).call({"from": owner, "value": native_in}))
        if actual_buy <= 0:
            raise ValueError("buy simulation returned zero")
        quoted_sell = int(wrapper.functions.quoteSell(token, source_type, source_data, actual_buy).call())
        if quoted_sell <= 0:
            raise ValueError("sell quote returned zero")
        round_trip_ratio = min(1.0, max(0.0, quoted_sell / native_in))
        return True, round_trip_ratio, ""
    except Exception as exc:
        return False, 0.0, str(exc).split("\n", 1)[0][:180]


def _topic_address(w3, topic) -> str:
    raw = topic.hex() if hasattr(topic, "hex") else str(topic)
    raw = raw[2:] if raw.startswith("0x") else raw
    return w3.to_checksum_address("0x" + raw[-40:])


def _resolve_robinhood_v4_pool_key(w3, pool_id: str) -> tuple:
    """Resolve the full Uniswap V4 PoolKey exactly as tagai-ui does."""
    from eth_abi import decode

    if not POOL_ID_RE.match(str(pool_id or "")):
        raise TrendingImportError("invalid Robinhood V4 pool id")
    manager = w3.to_checksum_address(
        config.TRENDING_IMPORT_CHAIN_CONFIG["robinhood"]["v4_pool_manager"]
    )
    signature = w3.keccak(
        text="Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)"
    )
    # Robinhood's public nodes cap eth_getLogs requests at 10m blocks. Search
    # newest-first in bounded inclusive windows so new trending pools resolve
    # immediately and older pools remain supported as the chain grows.
    latest_block = int(w3.eth.block_number)
    window_size = config.TRENDING_IMPORT_ROBINHOOD_V4_LOG_BLOCK_RANGE
    logs = []
    upper_block = latest_block
    while upper_block >= 0:
        lower_block = max(0, upper_block - window_size + 1)
        logs = w3.eth.get_logs({
            "address": manager,
            "topics": [signature, pool_id],
            "fromBlock": lower_block,
            "toBlock": upper_block,
        })
        if logs or lower_block == 0:
            break
        upper_block = lower_block - 1
    if not logs:
        raise TrendingImportError(f"PoolKey not found for poolId {pool_id}")
    log = logs[-1]
    topics = log["topics"]
    fee, tick_spacing, hooks, _sqrt_price, _tick = decode(
        ["uint24", "int24", "address", "uint160", "int24"], log["data"]
    )
    return (
        manager,
        _topic_address(w3, topics[2]),
        _topic_address(w3, topics[3]),
        int(fee),
        int(tick_spacing),
        w3.to_checksum_address(hooks),
    )


def _minimum_pool_liquidity_usd(chain: str, market_cap_usd) -> Decimal:
    if chain != "robinhood":
        return max(Decimal("10000"), _number(market_cap_usd) * Decimal("0.005"))
    chain_id = config.TRENDING_IMPORT_CHAIN_CONFIG[chain]["chain_id"]
    return tagai_chain_client.native_price_usd(chain_id) * Decimal(
        str(config.TRENDING_IMPORT_MIN_NATIVE_LIQUIDITY)
    )


def _dexscreener_payload(chain: str, address: str) -> dict:
    """Map Dexscreener pairs into the Gecko-shaped pool model used below."""
    response = requests.get(
        f"https://api.dexscreener.com/latest/dex/tokens/{address.lower()}",
        headers={"Accept": "application/json"},
        timeout=20,
    )
    response.raise_for_status()
    rows = response.json().get("pairs") or []
    data = []
    token_attrs = {}
    for row in rows:
        if str(row.get("chainId") or "").lower() != chain.lower():
            continue
        base = row.get("baseToken") or {}
        quote = row.get("quoteToken") or {}
        base_address = str(base.get("address") or "")
        quote_address = str(quote.get("address") or "")
        if address.lower() not in {base_address.lower(), quote_address.lower()}:
            continue
        target = base if base_address.lower() == address.lower() else quote
        info = row.get("info") or {}
        token_attrs = {
            "name": target.get("name") or "",
            "symbol": target.get("symbol") or "",
            "image_url": info.get("imageUrl") or "",
        }
        labels = [str(label) for label in (row.get("labels") or [])]
        dex_id = "-".join([str(row.get("dexId") or ""), *labels]).strip("-")
        created_at = ""
        if row.get("pairCreatedAt"):
            try:
                created_at = datetime.fromtimestamp(
                    float(row["pairCreatedAt"]) / 1000, timezone.utc,
                ).isoformat()
            except (TypeError, ValueError, OverflowError):
                pass
        data.append({
            "attributes": {
                "address": str(row.get("pairAddress") or ""),
                "name": " ".join(filter(None, [
                    f"{base.get('symbol', '')} / {quote.get('symbol', '')}", *labels,
                ])),
                "reserve_in_usd": (row.get("liquidity") or {}).get("usd") or 0,
                "volume_usd": {"h24": (row.get("volume") or {}).get("h24") or 0},
                "transactions": {"h24": (row.get("txns") or {}).get("h24") or {}},
                "pool_created_at": created_at,
            },
            "relationships": {
                "dex": {"data": {"id": dex_id}},
                "base_token": {"data": {"id": f"{chain}_{base_address.lower()}"}},
                "quote_token": {"data": {"id": f"{chain}_{quote_address.lower()}"}},
            },
        })
    return {
        "data": data,
        "included": [{"id": f"{chain}_{address.lower()}", "attributes": token_attrs}],
    }


def _gmgn_pool_payload(chain: str, address: str) -> dict:
    """Map GMGN pool_info into the Gecko-shaped model used by selection."""
    payload = gmgn_client.token_pool(chain, address)
    raw = payload.get("data") if isinstance(payload, dict) else None
    rows = raw if isinstance(raw, list) else ([raw] if isinstance(raw, dict) else [])
    data = []
    for row in rows:
        pair = str(row.get("pool_address") or row.get("address") or "")
        base = str(row.get("base_address") or row.get("token0_address") or "")
        quote = str(row.get("quote_address") or row.get("token1_address") or "")
        created_at = ""
        if row.get("creation_timestamp"):
            try:
                created_at = datetime.fromtimestamp(
                    float(row["creation_timestamp"]), timezone.utc,
                ).isoformat()
            except (TypeError, ValueError, OverflowError):
                pass
        data.append({
            "attributes": {
                "address": pair,
                "name": str(row.get("exchange") or ""),
                "reserve_in_usd": row.get("liquidity") or 0,
                # pool_info deliberately does not invent activity metrics.
                "volume_usd": {"h24": 0},
                "transactions": {"h24": {}},
                "pool_created_at": created_at,
                "data_source": "gmgn",
            },
            "relationships": {
                "dex": {"data": {"id": str(row.get("exchange") or "")}},
                "base_token": {"data": {"id": f"{chain}_{base.lower()}"}},
                "quote_token": {"data": {"id": f"{chain}_{quote.lower()}"}},
            },
        })
    return {"data": data, "included": []}


def _fallback_pool_payload(chain: str, address: str, pools_url: str) -> dict:
    if chain == "robinhood":
        return _dexscreener_payload(chain, address)
    try:
        response = requests.get(
            pools_url, headers={"Accept": "application/json"}, timeout=20,
        )
        if response.status_code == 429:
            return _dexscreener_payload(chain, address)
        response.raise_for_status()
        return response.json()
    except (requests.RequestException, ValueError):
        return _dexscreener_payload(chain, address)


def discover_token(chain: str, address: str, market_cap_usd=0, *, w3=None, owner: str = "") -> dict:
    chain_cfg = config.TRENDING_IMPORT_CHAIN_CONFIG[chain]
    network = chain_cfg["gecko_network"]
    pools_url = (
        f"https://api.geckoterminal.com/api/v2/networks/{network}/tokens/"
        f"{address.lower()}/pools?include=base_token%2Cquote_token&page=1"
    )
    minimum_liquidity = _minimum_pool_liquidity_usd(chain, market_cap_usd)
    preferred_quotes = {
        address.lower(): score for address, score in chain_cfg["preferred_quotes"].items()
    }
    attrs = {}
    rejected = []

    def select(payload: dict, source: str) -> list[dict]:
        nonlocal attrs
        pools = payload.get("data", [])
        included = payload.get("included", [])
        token_id = address.lower()
        discovered_attrs = next((
            item.get("attributes", {}) for item in included
            if str(item.get("id", "")).rsplit("_", 1)[-1].lower() == token_id
        ), {})
        attrs = attrs or discovered_attrs
        selected = []
        for pool in pools:
            dex_id = pool.get("relationships", {}).get("dex", {}).get("data", {}).get("id", "")
            pool_attrs = pool.get("attributes", {})
            version = _dex_version(dex_id)
            reserve = _number(pool_attrs.get("reserve_in_usd"))
            pair = str(pool_attrs.get("address") or "")
            base_token = _relationship_address(pool, "base_token")
            quote_token = _relationship_address(pool, "quote_token")
            if address.lower() not in {base_token.lower(), quote_token.lower()}:
                continue
            paired_token = quote_token if base_token.lower() == address.lower() else base_token
            transactions = pool_attrs.get("transactions", {}).get("h24", {})
            candidate = {
                "pair": pair,
                "dex_version": version,
                "dex_id": str(dex_id),
                "liquidity_usd": float(reserve),
                "volume_24h": float(_number(pool_attrs.get("volume_usd", {}).get("h24"))),
                "tx_count_24h": int(transactions.get("buys") or 0) + int(transactions.get("sells") or 0),
                "age_score": _age_score(pool_attrs.get("pool_created_at")),
                "paired_token": paired_token,
                "quote_score": preferred_quotes.get(paired_token.lower(), 0.25),
                "fee_pct": _pool_fee_pct(chain, version, pool_attrs.get("name", "")),
                "execution_score": 0.5,
                "data_source": source,
            }
            executable = (
                chain_cfg["dex_fragment"] in str(dex_id).lower()
                and version
                and reserve >= minimum_liquidity
                and (
                    ADDRESS_RE.match(pair)
                    or (
                        chain == "robinhood" and version == 4
                        and config.TRENDING_IMPORT_ALLOW_ROBINHOOD_V4
                        and POOL_ID_RE.match(pair)
                    )
                )
            )
            if not executable:
                continue
            should_preflight = chain == "bsc" or (
                w3 is not None
                and bool(owner)
                and str(chain_cfg.get("imported_swap_wrapper") or "").lower()
                != ZERO_ADDRESS.lower()
            )
            if should_preflight:
                if w3 is None or not owner:
                    raise TrendingImportError(f"{chain} pool preflight requires Web3 and owner")
                valid, execution_score, error = _preflight_import_pool(
                    w3, chain, address, candidate, owner,
                )
                candidate["execution_score"] = execution_score
                candidate["preflight_error"] = error
                if not valid:
                    rejected.append(f"{pair}:{error}")
                    continue
            selected.append(candidate)
        return selected

    primary_error = None
    try:
        matches = select(_gmgn_pool_payload(chain, address), "gmgn")
    except (gmgn_client.GMGNError, ValueError, AttributeError) as exc:
        primary_error = exc
        matches = []
    if not matches:
        try:
            matches = select(
                _fallback_pool_payload(chain, address, pools_url), "fallback",
            )
        except (requests.RequestException, ValueError, AttributeError) as exc:
            detail = f"GMGN: {primary_error}; fallback: {exc}" if primary_error else str(exc)
            raise TrendingImportError(f"DEX pool lookup failed: {detail}") from exc
    if not matches:
        detail = "; ".join(rejected[:3])
        raise TrendingImportError(
            f"no executable TagAI DEX pool with at least ${minimum_liquidity:.0f} liquidity"
            + (f": {detail}" if detail else "")
        )
    ranked = score_pools(matches)
    selected = ranked[0]
    return {
        "name": str(attrs.get("name") or ""),
        "symbol": str(attrs.get("symbol") or ""),
        "logo": str(attrs.get("image_url") or ""),
        "pair": selected["pair"],
        "dex_version": selected["dex_version"],
        "pool_score": selected["score"],
        "pool_fee_pct": selected["fee_pct"],
        "round_trip_ratio": selected["execution_score"],
        "pool_liquidity_usd": selected["liquidity_usd"],
        "pool_volume_24h": selected["volume_24h"],
        "pool_tx_count_24h": selected["tx_count_24h"],
        "paired_token": selected["paired_token"],
        "pool_data_source": selected["data_source"],
        "candidate_pools": len(ranked),
        "minimum_liquidity_usd": float(minimum_liquidity),
        "selection_strategy_version": POOL_SELECTION_STRATEGY_VERSION,
    }


def _latest(chain: str, token: str) -> dict:
    latest = {}
    for row in read_jsonl(config.TRENDING_IMPORT_LEDGER):
        if row.get("chain") == chain and str(row.get("token", "")).lower() == token.lower():
            latest.update(row)
            if row.get("event") == "tx_abandoned":
                # Preserve validated metadata across retries, but never reuse
                # a deliberately abandoned raw transaction or its receipt data.
                for field in ("raw_tx", "tx_hash", "community", "social_pool"):
                    latest.pop(field, None)
    return latest


def _ledger(event: str, chain: str, token: str, **fields) -> None:
    append_jsonl(config.TRENDING_IMPORT_LEDGER, {
        "at": _utc_now(), "event": event, "chain": chain,
        "chain_id": config.TRENDING_IMPORT_CHAIN_CONFIG[chain]["chain_id"],
        "token": token, **fields,
    })


def _reconcile_api_registration(chain: str, token: str, state: dict | None = None) -> dict:
    """Verify legacy ledger success against TagAI before treating it as final."""
    state = dict(_latest(chain, token) if state is None else state)
    if state.get("event") != "api_registered" or state.get("verified") is True:
        return state
    tick = str(state.get("tick") or "")
    if not tick:
        raise TrendingImportError("api_registered ledger entry is missing tick")
    chain_id = config.TRENDING_IMPORT_CHAIN_CONFIG[chain]["chain_id"]
    detail = tagai_chain_client.community_detail(tick, chain_id)
    token_matches = str(detail.get("token") or "").lower() == token.lower()
    expected_community = str(state.get("community") or "").lower()
    community_matches = (
        not expected_community
        or str(detail.get("communityAddress") or "").lower() == expected_community
    )
    if token_matches and community_matches:
        _ledger(
            "api_registered", chain, token, tick=tick, verified=True,
            tx_hash=state.get("tx_hash"), community=state.get("community"),
            social_pool=state.get("social_pool"),
        )
        return {**state, "event": "api_registered", "verified": True}
    _ledger(
        "api_registration_missing", chain, token, tick=tick, verified=False,
        tx_hash=state.get("tx_hash"), community=state.get("community"),
        social_pool=state.get("social_pool"), metadata=state.get("metadata"),
        error="local api_registered entry is absent or mismatched in community/detail",
    )
    return {**state, "event": "api_registration_missing", "verified": False}


def _run_event(event: str, run_id: str, **fields) -> None:
    append_jsonl(config.TRENDING_IMPORT_RUN_LEDGER, {
        "at": _utc_now(), "event": event, "run_id": run_id, **fields,
    })


def _candidate_run_outcomes() -> dict[tuple[str, str], dict]:
    latest: dict[tuple[str, str], dict] = {}
    for row in read_jsonl(config.TRENDING_IMPORT_RUN_LEDGER):
        if row.get("event") not in {"candidate_error", "candidate_result"}:
            continue
        chain = str(row.get("chain") or "")
        token = str(row.get("token") or "").lower()
        if not chain or not token:
            continue
        latest[(chain, token)] = row
    return latest


def _recent_run_outcomes(outcomes: dict[tuple[str, str], dict]) -> dict[tuple[str, str], datetime]:
    latest: dict[tuple[str, str], datetime] = {}
    for key, row in outcomes.items():
        if (
            row.get("event") == "candidate_result"
            and row.get("status") not in {"skipped", "tagai_pool_index_pending"}
        ):
            continue
        # Legacy whole-chain circuit results should not keep candidates in a
        # cooldown after upgrading to precise, per-pool quarantine.
        if row.get("reason") == "tagai_pool_index_unhealthy":
            continue
        # A run accidentally launched from the test-only virtualenv cannot
        # make chain progress. Once the production interpreter is restored,
        # do not make candidates wait for this local dependency mistake.
        if "no module named 'web3'" in str(row.get("error") or "").lower():
            continue
        # Older clients collapsed ticker collisions into ``token_exists``.
        # The current client can resolve those with a deterministic fallback
        # ticker, so legacy outcomes must not keep recovery in cooldown.
        if str(row.get("error") or "").lower() == "token_exists":
            continue
        if (
            row.get("status") == "unexpected_error"
            and "execution reverted" in str(row.get("error") or "").lower()
            and "0x" in str(row.get("error") or "").lower()
        ):
            # These legacy rows came from build_transaction's hidden gas
            # estimate. The estimate is now explicit and retried, so release
            # affected candidates immediately instead of preserving cooldown.
            continue
        try:
            latest[key] = datetime.fromisoformat(str(row["at"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            pass
    return latest


POOL_INDEX_RETRY_BACKOFF_SECONDS = (1800, 7200, 21600, 86400)
TAGAI_TRANSIENT_VALIDATION_RETRY_BACKOFF_SECONDS = (1800, 7200, 21600, 86400)


def _is_pool_index_failure(row: dict) -> bool:
    return (
        row.get("event") == "api_registration_failed"
        and "selected pool not found" in str(row.get("error") or "").lower()
    )


def _is_tagai_transient_validation_failure(row: dict) -> bool:
    """Whether TagAI's own quote RPC failed while registering a confirmed import."""
    error = str(row.get("error") or "").lower()
    return any(marker in error for marker in (
        "could not coalesce error", "import_validation_unavailable", "import_validation_timeout",
    )) or (
        "pool quote/trade validation failed" in error
        and ("timeout" in error or "timed out" in error)
    )


def _tagai_transient_validation_failure_counts() -> dict[tuple[str, str], int]:
    """Count unresolved TagAI validation outages so retries do not burn each run."""
    counts: dict[tuple[str, str], int] = {}
    for row in read_jsonl(config.TRENDING_IMPORT_LEDGER):
        chain = str(row.get("chain") or "")
        token = str(row.get("token") or "").lower()
        if not chain or not token:
            continue
        key = (chain, token)
        if row.get("event") == "api_registered":
            counts.pop(key, None)
        elif row.get("event") == "api_registration_failed" and _is_tagai_transient_validation_failure(row):
            counts[key] = counts.get(key, 0) + 1
    return counts


def _tagai_transient_validation_circuit_open(
    failures: dict[tuple[str, str], int], chain: str,
) -> bool:
    """Avoid creating more confirmed imports while TagAI cannot register them."""
    return sum(1 for (failure_chain, _token) in failures if failure_chain == chain) >= (
        config.TRENDING_IMPORT_TAGAI_TRANSIENT_VALIDATION_CIRCUIT_FAILURES
    )


def _registration_service_unavailable(error: str) -> bool:
    """An API outage must not grow the queue of paid, unregistered communities."""
    value = str(error).lower()
    return bool(re.search(r"tagai http 5\d\d\b", value) or "tagai request failed" in value)


def _pool_index_failure_counts() -> dict[tuple[str, str], int]:
    """Count unresolved TagAI pool-index failures for per-token backoff."""
    counts: dict[tuple[str, str], int] = {}
    for row in read_jsonl(config.TRENDING_IMPORT_LEDGER):
        chain = str(row.get("chain") or "")
        token = str(row.get("token") or "").lower()
        if not chain or not token:
            continue
        key = (chain, token)
        if row.get("event") == "api_registered":
            counts.pop(key, None)
        elif _is_pool_index_failure(row):
            counts[key] = counts.get(key, 0) + 1
    return counts


def _candidate_cooldown_seconds(
    key: tuple[str, str], previous: dict, pool_index_failures: dict[tuple[str, str], int],
    transient_validation_failures: dict[tuple[str, str], int] | None = None,
) -> int:
    base = config.TRENDING_IMPORT_RETRY_COOLDOWN_SECONDS
    failure_text = str(previous.get("error") or previous.get("reason") or "").lower()
    if (
        config.TRENDING_IMPORT_CHAIN_CONFIG.get(key[0], {}).get("helper_version") == 2
        and "0x3511df7e" in failure_text
    ):
        # The retired helpers lost registrar permission when V2 was activated.
        # Retry those unsigned failures against V2 without deleting audit state.
        # New deployment failures use a decoded error, retaining normal backoff.
        return 0
    if _is_tagai_transient_validation_failure({"error": failure_text}):
        failures = max(1, (transient_validation_failures or {}).get(key, 1))
        backoff = TAGAI_TRANSIENT_VALIDATION_RETRY_BACKOFF_SECONDS[
            min(failures - 1, len(TAGAI_TRANSIENT_VALIDATION_RETRY_BACKOFF_SECONDS) - 1)
        ]
        return max(base, backoff)
    if key[0] != "robinhood" or "selected pool not found" not in failure_text:
        return base
    failures = max(1, pool_index_failures.get(key, 1))
    backoff = POOL_INDEX_RETRY_BACKOFF_SECONDS[
        min(failures - 1, len(POOL_INDEX_RETRY_BACKOFF_SECONDS) - 1)
    ]
    return max(base, backoff)


def _prioritize_unseen(chain: str, rows: list[dict], outcomes: dict[tuple[str, str], dict]) -> list[dict]:
    """Preserve rank order within unseen/seen groups while preventing starvation."""
    return sorted(
        rows,
        key=lambda row: (int((chain, row["token"].lower()) in outcomes),),
    )


def _latest_import_states() -> dict[tuple[str, str], dict]:
    """Return the latest resumable import state without repeatedly rereading the ledger."""
    latest: dict[tuple[str, str], dict] = {}
    for row in read_jsonl(config.TRENDING_IMPORT_LEDGER):
        chain = str(row.get("chain") or "")
        token = str(row.get("token") or "").lower()
        if not chain or not token:
            continue
        state = latest.setdefault((chain, token), {})
        state.update(row)
        if row.get("event") == "tx_abandoned":
            for field in ("raw_tx", "tx_hash", "community", "social_pool"):
                state.pop(field, None)
    return latest


def _quarantined_robinhood_pools(
    states: dict[tuple[str, str], dict] | None = None,
) -> set[str]:
    """Return only pools that TagAI has explicitly rejected as unindexed."""
    if not config.TRENDING_IMPORT_QUARANTINE_REJECTED_POOLS:
        return set()
    states = _latest_import_states() if states is None else states
    return {
        str(dict(state.get("metadata") or {}).get("pair") or "").lower()
        for (chain, _token), state in states.items()
        if chain == "robinhood"
        and _is_pool_index_failure(state)
        and dict(state.get("metadata") or {}).get("dex_version") != 4
        and _has_recoverable_import(state)
        and ADDRESS_RE.match(str(dict(state.get("metadata") or {}).get("pair") or ""))
    }


def _robinhood_pool_index_unhealthy(
    states: dict[tuple[str, str], dict] | None = None,
) -> bool:
    """Backward-compatible health signal; writes are no longer chain-blocked."""
    return bool(_quarantined_robinhood_pools(states))


def _prioritize_candidates(
    chain: str,
    rows: list[dict],
    outcomes: dict[tuple[str, str], dict],
    import_states: dict[tuple[str, str], dict],
) -> list[dict]:
    """Recover confirmed/signed imports first, then preserve unseen/seen rank order."""
    return sorted(
        rows,
        key=lambda row: (
            0 if (
                import_states.get((chain, row["token"].lower()), {}).get("event")
                != "api_registered"
                and _has_recoverable_import(
                    import_states.get((chain, row["token"].lower()), {}),
                )
            ) else 1,
            int((chain, row["token"].lower()) in outcomes),
        ),
    )


def _candidate_detail(row: dict) -> dict:
    return {
        "token": row["token"],
        "symbol": row.get("symbol") or "",
        "name": row.get("name") or "",
        "market_cap_usd": str(row.get("market_cap_usd") or 0),
        "best_rank": row.get("best_rank"),
        "rank_sources": list(row.get("rank_sources") or []),
        "status": "queued",
    }


def _error_status(error: str) -> str:
    """Classify operational errors without treating normal filtering as opaque failure."""
    value = str(error).lower()
    if "import deployment mismatch" in value or "unauthorizedregistrar" in value:
        return "import_deployment_mismatch"
    if "no executable tagai dex pool" in value:
        return "no_executable_pool"
    if "dex pool lookup failed" in value:
        return "dex_data_unavailable"
    if _is_tagai_transient_validation_failure({"error": value}):
        return "tagai_transient_validation_pending"
    if "timed out" in value or "timeout" in value:
        return "api_timeout"
    if "api_registration" in value or "tagai http" in value or "tagai request failed" in value:
        return "api_registration_failed"
    if _is_transient_rpc_error(value):
        return "rpc_unavailable"
    if "preflight reverted" in value:
        return "transaction_preflight_failed"
    if "contract read" in value and "failed after" in value:
        return "rpc_contract_read_failed"
    if "insufficient native balance" in value or "insufficient funds" in value:
        return "insufficient_balance"
    if "daily import transaction limit" in value:
        return "daily_limit_reached"
    if "signer_unavailable" in value:
        return "signer_unavailable"
    if "transaction reverted" in value:
        return "transaction_reverted"
    if "wrong chain" in value:
        return "rpc_chain_mismatch"
    return "unexpected_error"


def _call_signer_with_retry(operation, *, attempts: int = 3):
    """Retry only the transient Claw signer-unavailable condition."""
    for attempt in range(attempts):
        try:
            return operation()
        except PredictStatus as exc:
            if exc.status != "signer_unavailable" or attempt + 1 >= attempts:
                raise
            time.sleep(1)


def _call_chain_read_with_retry(operation, label: str, *, attempts: int = 3):
    """Retry transient RPC transport failures and occasional empty reverts."""
    for attempt in range(attempts):
        try:
            return operation()
        except Exception as exc:
            retryable = _is_empty_execution_revert(exc) or _is_transient_rpc_error(exc)
            if not retryable or attempt + 1 >= attempts:
                detail = str(exc).split("\n", 1)[0][:240]
                raise TrendingImportError(
                    f"{label} failed after {attempt + 1} attempt(s): {detail}"
                ) from exc
            time.sleep(0.5 * (attempt + 1))


def _estimate_import_gas(w3, chain: str, tx: dict) -> int:
    """Estimate import gas, tolerating known RPC-only empty-revert bugs."""
    try:
        return int(_call_chain_read_with_retry(
            lambda: w3.eth.estimate_gas(tx),
            "contract read import gas estimation",
        ))
    except TrendingImportError as exc:
        if (
            chain in IMPORT_GAS_FALLBACKS
            and _is_empty_execution_revert(exc)
        ):
            # create_call.call() has already succeeded at this point, so an
            # empty revert confined to eth_estimateGas is an RPC false
            # negative rather than a contract-level rejection.
            return IMPORT_GAS_FALLBACKS[chain]
        raise


def _exception_text(exc: Exception) -> str:
    """Keep structured signer diagnostics while preserving safe truncation."""
    if isinstance(exc, PredictStatus) and exc.error:
        return f"{exc.status}: {exc.error}"[:300]
    return str(exc)[:300]


def _daily_limit_reached(chain: str) -> bool:
    """Return whether this chain reached its rolling 24-hour transaction cap."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    count = 0
    for row in read_jsonl(config.TRENDING_IMPORT_LEDGER):
        if row.get("event") != "tx_persisted" or row.get("chain") != chain:
            continue
        try:
            count += datetime.fromisoformat(str(row["at"]).replace("Z", "+00:00")) >= cutoff
        except (KeyError, ValueError):
            pass
    return count >= config.TRENDING_IMPORT_MAX_DAILY


def _rpc_urls(chain: str) -> list[str]:
    """Return the primary and chain-specific fallback RPCs without duplicates."""
    chain_cfg = config.TRENDING_IMPORT_CHAIN_CONFIG[chain]
    urls = [str(chain_cfg["rpc"]).rstrip("/")]
    fallbacks = {
        "bsc": config.TRENDING_IMPORT_BSC_RPC_FALLBACKS,
        "robinhood": config.TRENDING_IMPORT_ROBINHOOD_RPC_FALLBACKS,
    }.get(chain, ())
    urls.extend(fallbacks)
    return list(dict.fromkeys(str(url).rstrip("/") for url in urls if url))


def _web3(chain: str):
    from web3 import Web3

    return Web3(_FallbackHTTPProvider(
        _rpc_urls(chain), timeout=config.TRENDING_IMPORT_RPC_TIMEOUT_SECONDS,
    ))


def _fee(w3, chain: str, owner: str) -> int:
    chain_cfg = config.TRENDING_IMPORT_CHAIN_CONFIG[chain]
    committee = w3.eth.contract(address=w3.to_checksum_address(chain_cfg["committee"]), abi=COMMITTEE_ABI)
    create_fee = int(committee.functions.getCreateCommunityFee().call())
    settings_fee = int(committee.functions.getCommunitySettingsFee().call())
    if chain_cfg.get("helper_version") in (2, 3):
        helper = w3.eth.contract(
            address=w3.to_checksum_address(chain_cfg["import_helper"]), abi=IMPORT_HELPER_ABI,
        )
        if int(helper.functions.VERSION().call()) != chain_cfg["helper_version"]:
            raise TrendingImportError("import deployment mismatch: unexpected ImportHelper version")
        pool_count = int(helper.functions.defaultPoolCount().call())
        if not 1 <= pool_count <= 6:
            raise TrendingImportError("import deployment mismatch: invalid default import pool count")
        # V2/V3 charge one settings fee per default pool and never create IPShare.
        return create_fee + settings_fee * pool_count
    if not chain_cfg["enhanced"]:
        return create_fee + settings_fee
    ipshare = w3.eth.contract(address=w3.to_checksum_address(chain_cfg["ipshare"]), abi=IPSHARE_ABI)
    share_fee = 0 if ipshare.functions.ipshareCreated(owner).call() else int(ipshare.functions.createFee().call())
    return create_fee + settings_fee * 2 + share_fee


def _check_import_deployment(w3, chain: str) -> None:
    """Reject a stale helper before discovery, signing, or broadcasting."""
    cfg = config.TRENDING_IMPORT_CHAIN_CONFIG[chain]
    if cfg.get("helper_version") not in (2, 3):
        return
    helper_address = w3.to_checksum_address(cfg["import_helper"])
    wrapper_address = w3.to_checksum_address(cfg["imported_swap_wrapper"])
    helper = w3.eth.contract(address=helper_address, abi=IMPORT_HELPER_ABI)
    wrapper = w3.eth.contract(address=wrapper_address, abi=IMPORTED_SWAP_WRAPPER_ABI)
    if str(helper.functions.swapWrapper().call()).lower() != wrapper_address.lower():
        raise TrendingImportError("import deployment mismatch: helper points to a different Wrapper")
    registrar = str(wrapper.functions.registrar().call())
    if registrar.lower() != helper_address.lower():
        raise TrendingImportError(
            f"import deployment mismatch: Wrapper registrar={registrar}, configured helper={helper_address}"
        )
    if cfg["helper_version"] == 3:
        agent_address = w3.to_checksum_address(cfg["agent_swap_wrapper"])
        if str(helper.functions.agentSwapWrapper().call()).lower() != agent_address.lower():
            raise TrendingImportError("import deployment mismatch: helper points to a different Agent Wrapper")
        agent = w3.eth.contract(address=agent_address, abi=IMPORTED_SWAP_WRAPPER_ABI)
        if str(agent.functions.operator().call()).lower() != helper_address.lower():
            raise TrendingImportError("import deployment mismatch: Agent Wrapper operator differs from helper")


def _token_already_imported(w3, chain: str, helper, token: str) -> bool:
    cfg = config.TRENDING_IMPORT_CHAIN_CONFIG[chain]
    if cfg.get("helper_version") in (2, 3):
        wrapper = w3.eth.contract(
            address=w3.to_checksum_address(cfg["imported_swap_wrapper"]), abi=IMPORTED_SWAP_WRAPPER_ABI,
        )
        imported = bool(wrapper.functions.getImportedMarket(token).call()[0])
        if cfg["helper_version"] == 3:
            agent = w3.eth.contract(
                address=w3.to_checksum_address(cfg["agent_swap_wrapper"]), abi=IMPORTED_SWAP_WRAPPER_ABI,
            )
            imported = imported or bool(agent.functions.getImportedMarket(token).call()[0])
        return imported
    return bool(cfg["enhanced"] and str(helper.functions.importerOf(token).call()).lower() != ZERO_ADDRESS.lower())


def _transaction_fee_fields(w3, chain: str) -> dict:
    """Use a chain-safe fee cap compatible with the configured signer."""
    gas_price = int(w3.eth.gas_price)
    if chain != "robinhood":
        return {"gasPrice": gas_price}

    latest = w3.eth.get_block("latest")
    base_fee = int(latest.get("baseFeePerGas") or 0)
    try:
        priority_fee = int(w3.eth.max_priority_fee)
    except Exception:
        priority_fee = max(0, gas_price - base_fee)
    # Robinhood's public RPC can report a zero priority fee. Keep a small
    # non-zero cushion and a two-block base-fee headroom so a transaction does
    # not become invalid between estimation, Claw signing, and broadcast.
    # The TagAI browser wallet uses EIP-1559, but the production Claw signer
    # currently rejects type-2 transactions. A sufficiently buffered legacy
    # gasPrice has the same validity requirement on this EVM chain and remains
    # compatible with that signer.
    priority_fee = max(priority_fee, gas_price - base_fee, 1_000_000)
    max_fee = max(gas_price * 2, base_fee * 2 + priority_fee)
    return {"gasPrice": max_fee}


def _create_import_call(helper, chain: str, token: str):
    if config.TRENDING_IMPORT_CHAIN_CONFIG[chain].get("helper_version") != 2 and chain == "robinhood":
        # Robinhood's deployed enhanced ImportHelper still exposes only the
        # calculator entrypoint (selector b4e0960c). tagai-ui currently ships
        # the BSC two-address ABI for both chains, but the configured
        # Robinhood helper does not implement that selector. Keep this
        # chain-specific until a two-address Robinhood helper is deployed and
        # its address is published.
        return helper.functions.createCommunityAndPool(
            token,
            config.TRENDING_IMPORT_CHAIN_CONFIG[chain]["hourly_tick_calculator"],
            b"",
        )
    return helper.functions.createCommunityAndPool(token, ZERO_ADDRESS)


def _receipt_data(helper, receipt, token: str, owner: str) -> dict:
    if int(receipt.status) != 1:
        raise TrendingImportError("import transaction reverted")
    events = helper.events.CommunityCreated().process_receipt(receipt)
    event = next((row for row in events if str(row["args"]["token"]).lower() == token.lower()), None)
    if not event or str(event["args"]["creator"]).lower() != owner.lower():
        raise TrendingImportError("CommunityCreated receipt event missing or mismatched")
    return {"community": str(event["args"]["community"]), "social_pool": str(event["args"]["pool"])}


def _register(chain: str, candidate: dict, metadata: dict, owner: str, tx_hash: str, created: dict) -> dict:
    chain_id = config.TRENDING_IMPORT_CHAIN_CONFIG[chain]["chain_id"]
    info = {
        "tick": metadata["tick"], "token": candidate["token"],
        "pair": metadata["pair"], "dexVersion": metadata["dex_version"],
        "decimals": 18, "createHash": tx_hash, "ethAddr": owner,
        "communityAddress": created["community"], "socialPoolAddress": created["social_pool"],
        "desc": metadata["name"] or candidate.get("name") or metadata["tick"],
        "logoUrl": metadata["logo"] or candidate.get("logo") or "", "tags": [],
    }
    try:
        tagai_chain_client.register_import(info, owner, chain_id)
    except tagai_chain_client.TagAIChainError as exc:
        if exc.code == "ticker_exists":
            # A ticker may be claimed after validation but before API
            # registration. Preserve the confirmed on-chain import and retry
            # under a deterministic, human-readable unique ticker.
            for suffix_length in (6, 8, 4):
                suffix = candidate["token"][-suffix_length:].upper()
                alternate = f"{metadata['tick'][:15 - suffix_length]}_{suffix}"
                detail = tagai_chain_client.community_detail(alternate, chain_id)
                if (
                    str(detail.get("token") or "").lower()
                    == candidate["token"].lower()
                    and str(detail.get("communityAddress") or "").lower()
                    == created["community"].lower()
                ):
                    metadata = {**metadata, "tick": alternate}
                    info["tick"] = alternate
                    break
                if not tagai_chain_client.ticker_used(alternate, chain_id):
                    metadata = {**metadata, "tick": alternate}
                    info["tick"] = alternate
                    tagai_chain_client.register_import(info, owner, chain_id)
                    break
            else:
                _ledger(
                    "api_registration_failed", chain, candidate["token"],
                    tick=metadata["tick"], tx_hash=tx_hash, metadata=metadata,
                    error="no conflict-free fallback ticker", **created,
                )
                raise TrendingImportError("no conflict-free fallback ticker") from exc
        elif exc.code == "token_exists":
            _ledger(
                "api_registration_conflict", chain, candidate["token"],
                tick=metadata["tick"], tx_hash=tx_hash, metadata=metadata,
                reason="token already exists in TagAI but exact readback is unavailable",
                **created,
            )
            return {
                "status": "skipped", "reason": "api_token_conflict",
                "chain": chain, "token": candidate["token"],
                "tick": metadata["tick"], "tx_hash": tx_hash,
            }
        else:
            _ledger(
                "api_registration_failed", chain, candidate["token"],
                tick=metadata["tick"], tx_hash=tx_hash, metadata=metadata,
                error=str(exc)[:300], **created,
            )
            raise
    except Exception as exc:
        _ledger(
            "api_registration_failed", chain, candidate["token"],
            tick=metadata["tick"], tx_hash=tx_hash, metadata=metadata,
            error=str(exc)[:300], **created,
        )
        raise
    _ledger(
        "api_registered", chain, candidate["token"], tick=metadata["tick"],
        tx_hash=tx_hash, verified=True, **created,
    )
    return {"status": "imported", "chain": chain, "token": candidate["token"], "tick": metadata["tick"], "tx_hash": tx_hash}


def _has_recoverable_import(latest: dict) -> bool:
    """Whether a prior signed/confirmed transaction can resume safely."""
    if latest.get("event") == "api_registration_conflict":
        return False
    if not latest.get("tx_hash"):
        return False
    return bool(latest.get("raw_tx") or (
        latest.get("community") and latest.get("social_pool")
    ))


def _refresh_failed_registration_pool(
    chain: str, candidate: dict, latest: dict, metadata: dict, *, w3, owner: str,
) -> dict:
    """Refresh a pool rejected by TagAI before replaying API registration."""
    registration_error = str(latest.get("error") or "").lower()
    invalid_route = "imported-token/weth pair" in registration_error
    # The Robinhood V4 index can retain an obsolete pool long enough for an
    # import transaction to be confirmed, then reject that route during its
    # own quote simulation with only an opaque revert.  Treat that response as
    # a stale route signal: re-discovery is safe because this branch only
    # replays API registration for an already-confirmed import, never signs a
    # replacement transaction.
    opaque_robinhood_v4_revert = (
        chain == "robinhood"
        and int(metadata.get("dex_version") or 0) == 4
        and "execution reverted" in registration_error
    )
    stale_selection = (
        metadata.get("selection_strategy_version") != POOL_SELECTION_STRATEGY_VERSION
    )
    server_missing = latest.get("event") == "api_registration_missing"
    needs_refresh = stale_selection or server_missing or (
        latest.get("event") == "api_registration_failed"
        and (
            "selected pool not found" in registration_error
            or invalid_route
            or opaque_robinhood_v4_revert
        )
    )
    if not needs_refresh:
        return metadata
    try:
        refreshed = discover_token(
            chain, candidate["token"], candidate.get("market_cap_usd") or 0,
            w3=w3, owner=owner,
        )
    except Exception:
        if invalid_route or opaque_robinhood_v4_revert or stale_selection or server_missing:
            # The durable route is known to be invalid.  Never replay it just
            # because a replacement pool is not currently discoverable.
            raise
        # Registration still needs to be retried with the durable metadata if
        # market-data refresh is temporarily unavailable.
        return metadata
    return {**metadata, **refreshed, "tick": metadata["tick"]}


def import_candidate(chain: str, candidate: dict, *, execute: bool) -> dict:
    token = candidate["token"]
    latest = _reconcile_api_registration(chain, token, _latest(chain, token))
    if latest.get("event") == "api_registered":
        return {"status": "already_registered", "chain": chain, "token": token, "tick": latest.get("tick")}
    w3 = _web3(chain)
    chain_cfg = config.TRENDING_IMPORT_CHAIN_CONFIG[chain]
    if int(w3.eth.chain_id) != int(chain_cfg["chain_id"]):
        raise TrendingImportError("RPC returned the wrong chain")
    signer_env = {
        "signer": "claw", "rpc": chain_cfg["rpc"],
        "claw_wallet_dir": str(config.TRENDING_IMPORT_WALLET_DIR),
    }
    owner = w3.to_checksum_address(
        _call_signer_with_retry(lambda: _signer_address(signer_env))
    )
    helper = w3.eth.contract(address=w3.to_checksum_address(chain_cfg["import_helper"]), abi=IMPORT_HELPER_ABI)
    checksum_token = w3.to_checksum_address(token)
    if not _has_recoverable_import(latest):
        if _call_chain_read_with_retry(
            lambda: _token_already_imported(w3, chain, helper, checksum_token),
            "contract read imported market",
        ):
            return {"status": "skipped", "reason": "already_imported", "chain": chain, "token": token}
        _check_import_deployment(w3, chain)
    confirmed = bool(latest.get("tx_hash") and latest.get("community") and latest.get("social_pool"))
    required_fee = 0 if confirmed else _call_chain_read_with_retry(
        lambda: _fee(w3, chain, owner), "contract read import fee",
    )
    if execute and not confirmed and int(w3.eth.get_balance(owner)) <= required_fee:
        raise TrendingImportError(
            f"insufficient native balance: balance={w3.eth.get_balance(owner)}, required_fee={required_fee} plus gas"
        )

    metadata = dict(latest.get("metadata") or {})
    # A token validated by the former liquidity-only selector, but not yet
    # signed, must be rescored rather than inheriting the stale pool choice.
    if not metadata or (
        not latest.get("tx_hash")
        and metadata.get("selection_strategy_version") != POOL_SELECTION_STRATEGY_VERSION
    ):
        contract = w3.eth.contract(address=w3.to_checksum_address(token), abi=ERC20_ABI)
        if int(contract.functions.decimals().call()) != 18:
            return {"status": "skipped", "reason": "unsupported_decimals", "chain": chain, "token": token}
        symbol = contract.functions.symbol().call()
        tick = sanitize_tick(symbol, chain=chain, token=token) if symbol else None
        if symbol and not tick:
            return {"status": "skipped", "reason": "invalid_tick", "chain": chain, "token": token}
        if tick and tagai_chain_client.ticker_used(tick, int(chain_cfg["chain_id"])):
            return {"status": "skipped", "reason": "ticker_used", "chain": chain, "token": token, "tick": tick}
        discovered = discover_token(
            chain,
            token,
            candidate["market_cap_usd"],
            w3=w3,
            owner=owner,
        )
        if not symbol:
            tick = sanitize_tick(discovered["symbol"], chain=chain, token=token)
        if not tick:
            return {"status": "skipped", "reason": "invalid_tick", "chain": chain, "token": token}
        if not symbol and tagai_chain_client.ticker_used(tick, int(chain_cfg["chain_id"])):
            return {"status": "skipped", "reason": "ticker_used", "chain": chain, "token": token, "tick": tick}
        metadata = {**discovered, "tick": tick}
        _ledger("validated", chain, token, tick=tick, market_cap_usd=str(candidate["market_cap_usd"]), metadata=metadata)

    if not execute:
        return {"status": "eligible", "chain": chain, "token": token, "tick": metadata["tick"]}
    if (
        chain == "robinhood"
        and not _has_recoverable_import(latest)
        and str(metadata.get("pair") or "").lower() in _quarantined_robinhood_pools()
    ):
        return {
            "status": "skipped", "reason": "tagai_pool_not_indexed",
            "chain": chain, "token": token, "tick": metadata["tick"],
            "pair": metadata["pair"],
        }
    if (
        chain == "robinhood"
        and metadata.get("dex_version") == 4
        and not config.TRENDING_IMPORT_ALLOW_ROBINHOOD_V4
        and not latest.get("tx_hash")
    ):
        return {
            "status": "skipped", "reason": "tagai_api_v4_registration_unsupported",
            "chain": chain, "token": token, "tick": metadata["tick"],
        }
    if not confirmed and _daily_limit_reached(chain):
        raise TrendingImportError("daily import transaction limit reached")

    if confirmed:
        metadata = _refresh_failed_registration_pool(
            chain, candidate, latest, metadata, w3=w3, owner=owner,
        )
        return _register(chain, candidate, metadata, owner, str(latest["tx_hash"]), {
            "community": latest["community"], "social_pool": latest["social_pool"],
        })

    if latest.get("raw_tx") and latest.get("tx_hash"):
        from web3.exceptions import TransactionNotFound
        try:
            receipt = w3.eth.get_transaction_receipt(latest["tx_hash"])
        except TransactionNotFound:
            try:
                w3.eth.send_raw_transaction(latest["raw_tx"])
            except Exception as exc:
                # A persisted transaction can be replaced by another wallet
                # transaction using the same nonce.  Re-broadcasting that raw
                # payload will then fail forever with ``nonce too low``.  Only
                # abandon it after confirming this token was not imported;
                # the recursive call rebuilds the transaction with the current
                # pending nonce while retaining the validated pool metadata.
                if (
                    "nonce too low" not in str(exc).lower()
                    or _call_chain_read_with_retry(
                        lambda: _token_already_imported(w3, chain, helper, checksum_token),
                        "contract read imported market after nonce conflict",
                    )
                ):
                    raise
                _ledger(
                    "tx_abandoned", chain, token,
                    tick=metadata["tick"], metadata=metadata,
                    superseded_tx_hash=str(latest["tx_hash"]),
                    reason="persisted transaction nonce was superseded",
                )
                return import_candidate(chain, candidate, execute=execute)
            receipt = w3.eth.wait_for_transaction_receipt(latest["tx_hash"], timeout=300)
        created = _receipt_data(helper, receipt, token, owner)
        metadata = _refresh_failed_registration_pool(
            chain, candidate, latest, metadata, w3=w3, owner=owner,
        )
        return _register(chain, candidate, metadata, owner, str(latest["tx_hash"]), created)
    if latest.get("tx_hash") and latest.get("community") and latest.get("social_pool"):
        metadata = _refresh_failed_registration_pool(
            chain, candidate, latest, metadata, w3=w3, owner=owner,
        )
        return _register(chain, candidate, metadata, owner, str(latest["tx_hash"]), {
            "community": latest["community"], "social_pool": latest["social_pool"],
        })

    value = required_fee
    if value > config.TRENDING_IMPORT_MAX_VALUE_WEI:
        raise TrendingImportError("community import fee exceeds configured transaction cap")
    try:
        tagai_chain_client.validate_import_pool(
            token, metadata["pair"], metadata["dex_version"], owner, int(chain_cfg["chain_id"]),
        )
    except tagai_chain_client.TagAIChainError as exc:
        if exc.code == "invalid_import_pool":
            raise TrendingImportError(f"no executable TagAI DEX pool: {exc}") from exc
        raise
    create_call = _create_import_call(helper, chain, w3.to_checksum_address(token))
    try:
        # The TagAI UI simulates/estimates the write before asking the wallet
        # to sign. Do the same so contract reverts are separated from fee-model
        # failures and no unusable raw transaction is persisted.
        _call_chain_read_with_retry(
            lambda: create_call.call({"from": owner, "value": value}),
            "import preflight",
        )
    except Exception as exc:
        detail = str(exc).split("\n", 1)[0][:240]
        if "0x3511df7e" in detail.lower():
            raise TrendingImportError(
                "import deployment mismatch: UnauthorizedRegistrar() during import preflight"
            ) from exc
        raise TrendingImportError(f"import preflight reverted: {detail}") from exc

    # Supplying a temporary gas value prevents web3.build_transaction() from
    # performing an implicit, unclassified estimate. The explicit estimate
    # below is retried and replaces this placeholder before signing.
    tx = create_call.build_transaction({
        "from": owner, "value": value, "nonce": w3.eth.get_transaction_count(owner, "pending"),
        "chainId": int(chain_cfg["chain_id"]), "gas": 10_000_000,
        **_transaction_fee_fields(w3, chain),
    })
    estimated_gas = _estimate_import_gas(w3, chain, tx)
    tx["gas"] = int(estimated_gas * 12 // 10)
    _check_transaction_balance(w3, owner, tx)
    if str(tx.get("to", "")).lower() != str(chain_cfg["import_helper"]).lower():
        raise TrendingImportError("unexpected import transaction target")
    signer_env["rpc"] = str(getattr(w3.provider, "endpoint_uri", signer_env["rpc"]))
    signed = _call_signer_with_retry(lambda: _sign_tx(signer_env, tx))
    _ledger("tx_persisted", chain, token, tick=metadata["tick"], metadata=metadata,
            tx_hash=signed["txHash"], raw_tx=signed["rawTx"], value_wei=str(value), signer=owner)
    sent = w3.eth.send_raw_transaction(signed["rawTx"])
    receipt = w3.eth.wait_for_transaction_receipt(sent, timeout=300)
    created = _receipt_data(helper, receipt, token, owner)
    tx_hash = receipt.transactionHash.hex()
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash
    _ledger("tx_confirmed", chain, token, tick=metadata["tick"], metadata=metadata, tx_hash=tx_hash, **created)
    return _register(chain, candidate, metadata, owner, tx_hash, created)


def _check_transaction_balance(w3, owner: str, tx: dict) -> None:
    """Check the RPC's upfront gas requirement before persisting a signature."""
    required = int(tx.get("value", 0)) + int(tx["gas"]) * int(
        tx.get("maxFeePerGas", tx.get("gasPrice", 0))
    )
    balance = min(w3.eth.get_balance(owner, "latest"), w3.eth.get_balance(owner, "pending"))
    if balance < required:
        raise TrendingImportError(
            f"insufficient native balance: balance={balance}, required_total={required}, "
            f"shortfall={required - balance}"
        )


def run(*, execute: bool | None = None) -> dict:
    if not config.TRENDING_IMPORT_ENABLED:
        return {"status": "disabled", "chains": {}}
    if not config.GMGN_API_KEY:
        return {"status": "skipped", "reason": "GMGN_API_KEY is not configured", "chains": {}}
    execute = config.TRENDING_IMPORT_EXECUTE if execute is None else bool(execute)
    run_id = _utc_now()
    result: dict = {
        "status": "ok", "execute": execute, "run_id": run_id,
        "stages": {"snapshot": "running", "recovery": "pending", "import": "pending"},
        "chains": {},
    }
    eligible_by_chain: dict[str, list[dict]] = {}
    details_by_chain: dict[str, dict[str, dict]] = {}
    _run_event("run_started", run_id, execute=execute)
    for chain in ("bsc", "robinhood"):
        chain_result = {
            "top_count": 0, "eligible_count": 0, "attempted_count": 0,
            "cooldown_count": 0, "terminal_skip_count": 0,
            "below_threshold_count": 0, "recovery_count": 0, "deferred_count": 0,
            "items": [], "errors": [], "candidate_details": [],
            "status_counts": {}, "budget_seconds": max(
                30, config.TRENDING_IMPORT_MAX_RUN_SECONDS // 2,
            ), "spent_seconds": 0.0,
        }
        result["chains"][chain] = chain_result
        try:
            ranked, source_errors = collect_rank_candidates(chain, config.TRENDING_IMPORT_LIMIT)
            chain_result["top_count"] = len(ranked)
            chain_result["source_count"] = len(RANK_INTERVALS) * 2 + int(any(
                source.get("source") == "fomo:trending"
                for row in ranked for source in row.get("rank_sources", [])
            ))
            chain_result["errors"].extend(source_errors)
            eligible_by_chain[chain] = [
                row for row in ranked
                if row["market_cap_usd"] > config.TRENDING_IMPORT_MIN_MARKET_CAP_USD
            ]
            chain_result["eligible_count"] = len(eligible_by_chain[chain])
            chain_result["below_threshold_count"] = len(ranked) - len(eligible_by_chain[chain])
            chain_result["candidate_details"] = [
                _candidate_detail(row) for row in eligible_by_chain[chain]
            ]
            details_by_chain[chain] = {
                row["token"].lower(): row for row in chain_result["candidate_details"]
            }
            _run_event(
                "snapshot_collected", run_id, chain=chain,
                top_count=len(ranked), eligible_count=len(eligible_by_chain[chain]),
                below_threshold_count=chain_result["below_threshold_count"],
                candidates=chain_result["candidate_details"], errors=source_errors,
            )
        except Exception as exc:
            chain_result["errors"].append({"error": str(exc)[:300]})
            eligible_by_chain[chain] = []
            details_by_chain[chain] = {}
            _run_event("snapshot_failed", run_id, chain=chain, error=str(exc)[:300])

    result["stages"]["snapshot"] = "completed"
    outcomes = _candidate_run_outcomes()
    import_states = _latest_import_states()
    registration_circuit = {
        chain: any(
            state_chain == chain and state.get("event") == "api_registration_failed"
            and _has_recoverable_import(state)
            and _registration_service_unavailable(state.get("error", ""))
            for (state_chain, _token), state in import_states.items()
        ) for chain in result["chains"]
    }
    for chain, is_open in registration_circuit.items():
        result["chains"][chain]["tagai_registration_circuit_open"] = is_open
    quarantined_robinhood_pools = _quarantined_robinhood_pools(import_states)
    result["chains"]["robinhood"]["pool_index_circuit_open"] = False
    result["chains"]["robinhood"]["quarantined_pool_count"] = len(
        quarantined_robinhood_pools
    )
    # Recovery is independent of the current GMGN ranking. Once a transaction
    # has been signed or confirmed, API registration must finish even if the
    # token drops out of the next snapshot; this also prevents duplicate txs.
    for (chain, token), state in import_states.items():
        if chain not in eligible_by_chain or state.get("event") == "api_registered":
            continue
        if not _has_recoverable_import(state):
            continue
        if token in details_by_chain[chain]:
            continue
        metadata = dict(state.get("metadata") or {})
        candidate = {
            "token": state.get("token") or token,
            "market_cap_usd": Decimal(0),
            "name": metadata.get("name") or "",
            "symbol": metadata.get("symbol") or state.get("tick") or "",
            "logo": metadata.get("logo") or "",
            "best_rank": None,
            "rank_sources": [{"source": "recovery_ledger", "rank": None}],
        }
        eligible_by_chain[chain].append(candidate)
        detail = _candidate_detail(candidate)
        result["chains"][chain]["candidate_details"].append(detail)
        details_by_chain[chain][token] = detail
        result["chains"][chain]["recovery_count"] += 1
    recent = _recent_run_outcomes(outcomes)
    pool_index_failures = _pool_index_failure_counts()
    transient_validation_failures = _tagai_transient_validation_failure_counts()
    robinhood_validation_circuit_open = _tagai_transient_validation_circuit_open(
        transient_validation_failures, "robinhood",
    )
    result["chains"]["robinhood"]["tagai_validation_circuit_open"] = (
        robinhood_validation_circuit_open
    )
    result["chains"]["robinhood"]["tagai_validation_failure_count"] = sum(
        1 for (chain, _token) in transient_validation_failures if chain == "robinhood"
    )
    for chain, rows in eligible_by_chain.items():
        eligible_by_chain[chain] = _prioritize_candidates(
            chain, rows, outcomes, import_states,
        )
    result["stages"]["recovery"] = "completed"
    result["stages"]["import"] = "running"
    now = datetime.now(timezone.utc)
    max_rank = max((len(rows) for rows in eligible_by_chain.values()), default=0)
    budget_reached: dict[str, bool] = {"bsc": False, "robinhood": False}
    spent: dict[str, float] = {"bsc": 0.0, "robinhood": 0.0}
    # Start with Robinhood, then alternate chains by rank. A slow BSC candidate
    # can no longer prevent Robinhood from being observed in the same run. Each
    # chain has an independent budget, so a slow chain cannot starve the other.
    for index in range(max_rank):
        for chain in ("robinhood", "bsc"):
            rows = eligible_by_chain[chain]
            chain_result = result["chains"][chain]
            details = details_by_chain[chain]
            if index >= len(rows) or chain_result["attempted_count"] >= config.TRENDING_IMPORT_MAX_ATTEMPTS_PER_CHAIN:
                continue
            remaining_seconds = chain_result["budget_seconds"] - spent[chain]
            if remaining_seconds < config.TRENDING_IMPORT_ATTEMPT_RESERVE_SECONDS:
                budget_reached[chain] = True
                continue
            candidate = rows[index]
            token = candidate["token"]
            detail = details[token.lower()]
            try:
                latest = _reconcile_api_registration(
                    chain, token, _latest(chain, token),
                )
            except Exception as exc:
                error = _exception_text(exc)
                chain_result["errors"].append({"token": token, "error": error})
                detail.update({
                    "status": "api_registration_check_failed", "error": error,
                })
                continue
            if latest.get("event") == "api_registered":
                chain_result["items"].append({
                    "status": "already_registered", "chain": chain, "token": token,
                })
                detail["status"] = "already_registered"
                continue
            if registration_circuit[chain] and not _has_recoverable_import(latest):
                detail.update({
                    "status": "deferred_tagai_registration_circuit",
                    "reason": "confirmed imports await recovery after a TagAI registration service failure",
                })
                continue
            if (
                chain == "robinhood"
                and robinhood_validation_circuit_open
                and not _has_recoverable_import(latest)
            ):
                # Existing confirmed transactions must remain recoverable, but
                # a broken TagAI validator must never create a growing queue
                # of otherwise-valid on-chain communities.
                detail.update({
                    "status": "deferred_tagai_validation_circuit",
                    "reason": "TagAI transient pool validation circuit is open",
                })
                continue
            candidate_key = (chain, token.lower())
            previous = outcomes.get(candidate_key, {})
            if (
                previous.get("event") == "candidate_result"
                and previous.get("status") == "skipped"
                and previous.get("reason") in TERMINAL_SKIP_REASONS
            ):
                chain_result["terminal_skip_count"] += 1
                detail.update({
                    "status": "terminal_skip", "reason": previous.get("reason"),
                })
                continue
            cooldown_seconds = _candidate_cooldown_seconds(
                candidate_key, previous, pool_index_failures, transient_validation_failures,
            )
            if recent.get(
                candidate_key, datetime.min.replace(tzinfo=timezone.utc),
            ) >= now - timedelta(seconds=cooldown_seconds):
                chain_result["cooldown_count"] += 1
                detail.update({
                    "status": "retry_cooldown",
                    "reason": previous.get("reason") or previous.get("error") or "recent_attempt",
                    "cooldown_seconds": cooldown_seconds,
                })
                continue
            chain_result["attempted_count"] += 1
            attempt_started = time.monotonic()
            try:
                item = import_candidate(chain, candidate, execute=execute)
                chain_result["items"].append(item)
                if item.get("status") == "skipped":
                    detail.update({
                        "status": "terminal_skip" if item.get("reason") in TERMINAL_SKIP_REASONS else "blocked",
                        "reason": item.get("reason"),
                    })
                else:
                    detail.update({
                        key: value for key, value in item.items()
                        if key not in {"chain", "token"}
                    })
                _run_event("candidate_result", run_id, chain=chain, token=token, **{
                    key: value for key, value in item.items() if key not in {"chain", "token"}
                })
            except Exception as exc:
                error = _exception_text(exc)
                known_pool_index_failure = (
                    chain == "robinhood"
                    and _is_pool_index_failure(import_states.get(candidate_key, {}))
                    and "selected pool not found" in error.lower()
                )
                if known_pool_index_failure:
                    item = {
                        "token": token, "chain": chain,
                        "status": "tagai_pool_index_pending", "error": error,
                    }
                    chain_result["items"].append(item)
                    detail.update({
                        "status": "tagai_pool_index_pending", "error": error,
                    })
                    _run_event(
                        "candidate_result", run_id, chain=chain, token=token,
                        status="tagai_pool_index_pending", error=error,
                        reason="selected pool not found in TagAI index",
                    )
                    continue
                error_status = _error_status(error)
                if _registration_service_unavailable(error):
                    registration_circuit[chain] = True
                    chain_result["tagai_registration_circuit_open"] = True
                error_item = {"token": token, "status": error_status, "error": error}
                chain_result["errors"].append(error_item)
                detail.update({"status": error_status, "error": error})
                _run_event(
                    "candidate_error", run_id, chain=chain, token=token,
                    status=error_status, error=error,
                )
            finally:
                spent[chain] += time.monotonic() - attempt_started
                # Record the hard boundary immediately.  Previously the flag
                # was set only when another candidate happened to be reached,
                # so a chain that exhausted its budget on the fifteenth (last)
                # attempt was incorrectly reported as within budget.
                if spent[chain] >= chain_result["budget_seconds"]:
                    budget_reached[chain] = True
    for chain, rows in eligible_by_chain.items():
        chain_result = result["chains"][chain]
        for detail in chain_result["candidate_details"]:
            if detail["status"] != "queued":
                continue
            if budget_reached[chain]:
                detail["status"] = "deferred_chain_budget"
            elif chain_result["attempted_count"] >= config.TRENDING_IMPORT_MAX_ATTEMPTS_PER_CHAIN:
                detail["status"] = "deferred_attempt_limit"
            else:
                detail["status"] = "deferred_queue"
        chain_result["deferred_count"] = sum(
            str(row["status"]).startswith("deferred_")
            for row in chain_result["candidate_details"]
        )
        chain_result["spent_seconds"] = round(spent[chain], 3)
        chain_result["status_counts"] = dict(Counter(
            row["status"] for row in chain_result["candidate_details"]
        ))
        operational_errors = [
            row for row in chain_result["errors"]
            if row.get("status") not in {
                "no_executable_pool",
            }
        ]
        if operational_errors:
            result["status"] = "partial_failure"
    result["stages"]["import"] = "completed"
    result["deadline_reached"] = any(budget_reached.values())
    result["chain_budget_reached"] = budget_reached
    _run_event(
        "run_completed", run_id, status=result["status"],
        stages=result["stages"], deadline_reached=result["deadline_reached"],
        chain_budget_reached=budget_reached, chains=result["chains"],
    )
    return result

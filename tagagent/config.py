from __future__ import annotations

import os

from pathlib import Path

HOME = Path.home()

PKG_ROOT = Path(__file__).resolve().parents[1]

RUNTIME_ROOT = PKG_ROOT / "runtime"

HERMES_ENV_PATH = PKG_ROOT / ".env"

def _dotenv_value(path: Path, name: str) -> str:
    try:
        with Path(path).open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() == name:
                    return value.strip().strip("'\"")
    except OSError:
        return ""
    return ""

def hermes_env(name: str, default: str = "") -> str:
    """Env var first, then ~/.hermes/.env, then caller default."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    value = _dotenv_value(HERMES_ENV_PATH, name)
    if value:
        return value
    return default

TAGAI_PREDICT_RPC = hermes_env(
    "TAGAI_PREDICT_RPC", "https://bsc-dataseed.binance.org"
)

CLAW_WALLET_DIR = Path(
    hermes_env(
        "CLAW_WALLET_DIR",
        str(PKG_ROOT / "wallet"),
    )
)

TOKEN_LAUNCH_API_BASE = hermes_env(
    "TOKEN_LAUNCH_API_BASE", "https://bsc-api.tagai.fun"
).rstrip("/")

GMGN_API_KEY = hermes_env("GMGN_API_KEY")

GMGN_API_BASE = hermes_env("GMGN_API_BASE", "https://openapi.gmgn.ai").rstrip("/")

GMGN_API_USE_PROXY = hermes_env("GMGN_API_USE_PROXY", "0") == "1"

GMGN_API_FALLBACK_BASES = tuple(
    url.strip().rstrip("/")
    for url in hermes_env("GMGN_API_FALLBACK_BASES", "").split(",")
    if url.strip()
)

GMGN_API_MAX_ATTEMPTS = max(1, int(hermes_env("GMGN_API_MAX_ATTEMPTS", "3")))

GMGN_API_RETRY_BACKOFF_SECONDS = max(
    0.0, float(hermes_env("GMGN_API_RETRY_BACKOFF_SECONDS", "0.5"))
)

TRENDING_IMPORT_ENABLED = hermes_env("TRENDING_IMPORT_ENABLED", "1") == "1"

TRENDING_IMPORT_EXECUTE = hermes_env("TRENDING_IMPORT_EXECUTE", "0") == "1"

TRENDING_IMPORT_LIMIT = int(hermes_env("TRENDING_IMPORT_LIMIT", "50"))

TRENDING_IMPORT_MIN_MARKET_CAP_USD = int(
    hermes_env("TRENDING_IMPORT_MIN_MARKET_CAP_USD", "300000")
)

TRENDING_IMPORT_MAX_VALUE_WEI = int(
    hermes_env("TRENDING_IMPORT_MAX_VALUE_WEI", "200000000000000000")
)

TRENDING_IMPORT_MAX_DAILY = int(hermes_env("TRENDING_IMPORT_MAX_DAILY", "100"))

TRENDING_IMPORT_MAX_ATTEMPTS_PER_CHAIN = max(
    1, int(hermes_env("TRENDING_IMPORT_MAX_ATTEMPTS_PER_CHAIN", "15"))
)

TRENDING_IMPORT_MAX_RUN_SECONDS = max(
    # The worker alternates two chains serially.  Five minutes keeps the
    # scheduled run inside its monitoring window while still allowing a full
    # 15-candidate safety cap when RPCs are healthy.
    60, int(hermes_env("TRENDING_IMPORT_MAX_RUN_SECONDS", "300"))
)

TRENDING_IMPORT_ATTEMPT_RESERVE_SECONDS = max(
    1, int(hermes_env("TRENDING_IMPORT_ATTEMPT_RESERVE_SECONDS", "15"))
)

TRENDING_IMPORT_RETRY_COOLDOWN_SECONDS = max(
    0, int(hermes_env("TRENDING_IMPORT_RETRY_COOLDOWN_SECONDS", "1800"))
)

TRENDING_IMPORT_PAUSE_ROBINHOOD_ON_POOL_INDEX_FAILURE = (
    hermes_env("TRENDING_IMPORT_PAUSE_ROBINHOOD_ON_POOL_INDEX_FAILURE", "1") == "1"
)

TRENDING_IMPORT_QUARANTINE_REJECTED_POOLS = (
    hermes_env(
        "TRENDING_IMPORT_QUARANTINE_REJECTED_POOLS",
        "1" if TRENDING_IMPORT_PAUSE_ROBINHOOD_ON_POOL_INDEX_FAILURE else "0",
    ) == "1"
)

TRENDING_IMPORT_ALLOW_ROBINHOOD_V4 = (
    hermes_env("TRENDING_IMPORT_ALLOW_ROBINHOOD_V4", "1") == "1"
)

TRENDING_IMPORT_MIN_NATIVE_LIQUIDITY = max(
    0.0, float(hermes_env("TRENDING_IMPORT_MIN_NATIVE_LIQUIDITY", "1"))
)

TRENDING_IMPORT_API_BASE = hermes_env(
    "TRENDING_IMPORT_API_BASE", TOKEN_LAUNCH_API_BASE
).rstrip("/")

TRENDING_IMPORT_BSC_RPC_FALLBACKS = tuple(
    url.strip().rstrip("/")
    for url in hermes_env(
        "TRENDING_IMPORT_BSC_RPC_FALLBACKS",
        (
            "https://bsc-dataseed.bnbchain.org,"
            "https://bsc-dataseed-public.bnbchain.org,"
            "https://bsc-rpc.publicnode.com"
        ),
    ).split(",")
    if url.strip()
)

TRENDING_IMPORT_ROBINHOOD_RPC_FALLBACKS = tuple(
    url.strip().rstrip("/")
    for url in hermes_env(
        "TRENDING_IMPORT_ROBINHOOD_RPC_FALLBACKS",
        "https://robinhood-rpc.publicnode.com",
    ).split(",")
    if url.strip()
)

TRENDING_IMPORT_RPC_RETRIES_PER_ENDPOINT = max(
    1, int(hermes_env("TRENDING_IMPORT_RPC_RETRIES_PER_ENDPOINT", "2"))
)

TRENDING_IMPORT_RPC_RETRY_BACKOFF_SECONDS = max(
    0.0, float(hermes_env("TRENDING_IMPORT_RPC_RETRY_BACKOFF_SECONDS", "0.25"))
)

TRENDING_IMPORT_RPC_TIMEOUT_SECONDS = max(
    1.0, float(hermes_env("TRENDING_IMPORT_RPC_TIMEOUT_SECONDS", "12"))
)

TRENDING_IMPORT_ROBINHOOD_V4_LOG_BLOCK_RANGE = max(
    1, min(9_000_000, int(hermes_env(
        "TRENDING_IMPORT_ROBINHOOD_V4_LOG_BLOCK_RANGE", "9000000"
    )))
)

TRENDING_IMPORT_WALLET_DIR = Path(
    hermes_env("TRENDING_IMPORT_WALLET_DIR", str(CLAW_WALLET_DIR))
)

TRENDING_IMPORT_LEDGER = RUNTIME_ROOT / "trending-import" / "import-log.jsonl"

TRENDING_IMPORT_RUN_LEDGER = RUNTIME_ROOT / "trending-import" / "run-log.jsonl"

TRENDING_IMPORT_FOMO_FALLBACK_ENABLED = (
    hermes_env("TRENDING_IMPORT_FOMO_FALLBACK_ENABLED", "1") == "1"
)

TRENDING_IMPORT_FOMO_SNAPSHOT = Path(
    hermes_env(
        "TRENDING_IMPORT_FOMO_SNAPSHOT",
        str(RUNTIME_ROOT / "trending-import" / "fomo-trending.json"),
    )
)

TRENDING_IMPORT_FOMO_SNAPSHOT_MAX_AGE_SECONDS = max(
    60, int(hermes_env("TRENDING_IMPORT_FOMO_SNAPSHOT_MAX_AGE_SECONDS", "1200"))
)

TRENDING_IMPORT_TAGAI_TRANSIENT_VALIDATION_CIRCUIT_FAILURES = max(
    1, int(hermes_env("TRENDING_IMPORT_TAGAI_TRANSIENT_VALIDATION_CIRCUIT_FAILURES", "3"))
)

TRENDING_IMPORT_CHAIN_CONFIG = {
    "bsc": {
        "chain_id": 56,
        "rpc": hermes_env("TRENDING_IMPORT_BSC_RPC", "https://bsc-dataseed.bnbchain.org"),
        "gecko_network": "bsc",
        "dex_fragment": "pancakeswap",
        "wrapped_native": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
        "preferred_quotes": {
            "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c": 1.0,
            "0x55d398326f99059fF775485246999027B3197955": 0.95,
            "0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d": 0.95,
        },
        "v2_router": "0x10ED43C718714eb63d5aA57B78B54704E256024E",
        "v3_router": "0x13f4EA83D0bd40E75C8222255bc855a974568Dd4",
        "v3_quoter": "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997",
        "imported_swap_wrapper": "0xDfFc699FB095a693708E3c15e6F0a224cbbCc98F",
        "import_helper": "0xDcCc7428338Db3a7480bEcAa9dAc2965425E0EDF",
        "helper_version": 3,
        "agent_swap_wrapper": "0x3F7146D3D234Ed4C2021DE1A5B14da8754e6C577",
        "committee": "0xe10F967DD356504EDB731612789D0D0f0ba2929f",
        "ipshare": "0x95450AaD4Cc195e03BB4791B7f6f04aC6D9BA922",
        "enhanced": False,
    },
    "robinhood": {
        "chain_id": 4663,
        "rpc": hermes_env(
            "TRENDING_IMPORT_ROBINHOOD_RPC",
            "https://rpc.mainnet.chain.robinhood.com",
        ),
        "gecko_network": "robinhood",
        "dex_fragment": "uniswap",
        "wrapped_native": "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
        "preferred_quotes": {
            "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73": 1.0,
        },
        "v2_router": "0x89e5DB8B5aA49aA85AC63f691524311AEB649eba",
        "v3_router": "0xCaf681a66D020601342297493863E78C959E5cb2",
        "v3_quoter": "0x33e885eD0Ec9bF04EcfB19341582aADCb4c8A9E7",
        "v4_pool_manager": "0x8366a39CC670B4001A1121B8F6A443A643e40951",
        "imported_swap_wrapper": "0x53e65DE68A0eB7f3662579F44Eb9849Ae5cA44ab",
        "import_helper": "0x70eD7C79d80dEDE1828a09B3c526df283111Cb82",
        "helper_version": 2,
        "hourly_tick_calculator": "0x3DC52C69C3C8be568372E16d50E9F3FEc796610c",
        "committee": "0x7B0ddC305C32AAEbabc0FE372a4460e9903e95D0",
        "ipshare": "0x8A7b0d80FA92699CE3e5bB2c8fE404D6733796d1",
        "enhanced": False,
    },
}

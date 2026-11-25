from __future__ import annotations

import json

import os

import subprocess

import re

from pathlib import Path

from typing import Any

from tagagent import config

CLAW_CHAIN_BY_ID = {56: "bsc", 4663: "robinhood"}

class PredictStatus(Exception):
    def __init__(self, status: str, error: str = ""):
        super().__init__(status)
        self.status = status
        self.error = error

def _signed_raw(signed) -> Any:
    return getattr(signed, "rawTransaction", None) or getattr(signed, "raw_transaction")

def _hex_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return "0x" + bytes(value).hex()
    hex_fn = getattr(value, "hex", None)
    if callable(hex_fn):
        text = hex_fn()
        return text if str(text).startswith("0x") else "0x" + str(text)
    return str(value)

def _json_tx_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_tx_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_tx_value(item) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return _hex_value(value)
    hex_fn = getattr(value, "hex", None)
    if callable(hex_fn):
        return _hex_value(value)
    return value

def _tx_payload(tx: dict) -> dict:
    payload = {}
    fields = {
        "from",
        "to",
        "data",
        "value",
        "nonce",
        "gas",
        "gasLimit",
        "gasPrice",
        "maxFeePerGas",
        "maxPriorityFeePerGas",
        "type",
        "accessList",
        "chainId",
    }
    for key, value in tx.items():
        if key not in fields:
            continue
        out_key = "gasLimit" if key == "gas" else key
        payload[out_key] = _json_tx_value(value)
    payload.setdefault("value", "0")
    payload.setdefault("data", "0x")
    return payload

def _read_wallet_address(wallet_dir: str) -> str:
    wallet_path = Path(wallet_dir)
    env_addr = config._dotenv_value(wallet_path / ".env", "TAGCLAW_ETH_ADDR")
    if env_addr:
        return env_addr
    try:
        identity = json.loads((wallet_path / "identity.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    addresses = identity.get("addresses") if isinstance(identity, dict) else {}
    if isinstance(addresses, dict):
        return str(addresses.get("bsc") or addresses.get("ethereum") or "")
    return ""

def _signer_address(env: dict) -> str:
    if env.get("signer") == "local":
        from eth_account import Account

        if not env.get("bsc_key"):
            raise PredictStatus("disabled")
        return Account.from_key(env["bsc_key"]).address
    address = _read_wallet_address(str(env.get("claw_wallet_dir") or ""))
    if not address:
        raise PredictStatus("signer_unavailable", "claw wallet address unavailable")
    return address

def _sanitize_signer_error(text: str) -> str:
    text = " ".join(str(text or "").split())
    text = re.sub(r"(?i)(CLAY_AGENT_TOKEN|AGENT_TOKEN|accesstoken|BSC_KEY)\s*[:=]\s*\S+", r"\1=<redacted>", text)
    return text[:300]

def _tx_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or value is None or value == "":
        raise ValueError(f"invalid {field}")
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text, 16) if text.lower().startswith("0x") else int(text)

def _claw_chain_name(chain_id: Any) -> str:
    try:
        normalized = _tx_int(chain_id, "chainId")
    except (TypeError, ValueError):
        raise PredictStatus("signer_unavailable", "invalid Claw transaction chainId")
    chain = CLAW_CHAIN_BY_ID.get(normalized)
    if not chain:
        raise PredictStatus("signer_unavailable", f"unsupported Claw transaction chainId: {normalized}")
    return chain

def _validate_claw_payload(payload: dict, expected_from: str) -> None:
    _claw_chain_name(payload.get("chainId"))
    for field in ("from", "to", "nonce", "gasLimit"):
        if payload.get(field) in {None, ""}:
            raise ValueError(f"missing transaction {field}")
    if _normalized_address(payload["from"]) != _normalized_address(expected_from):
        raise ValueError("transaction from does not match expected Claw signer")
    for field in ("nonce", "gasLimit", "value"):
        if _tx_int(payload.get(field, 0), field) < 0:
            raise ValueError(f"invalid transaction {field}")

    has_gas_price = payload.get("gasPrice") is not None
    has_max_fee = payload.get("maxFeePerGas") is not None
    has_priority_fee = payload.get("maxPriorityFeePerGas") is not None
    if has_gas_price == (has_max_fee or has_priority_fee):
        raise ValueError("transaction must contain exactly one complete fee model")
    if has_max_fee != has_priority_fee:
        raise ValueError("EIP-1559 transaction requires both fee fields")
    for field in ("gasPrice", "maxFeePerGas", "maxPriorityFeePerGas"):
        if payload.get(field) is not None and _tx_int(payload[field], field) < 0:
            raise ValueError(f"invalid transaction {field}")

    if payload.get("type") is not None:
        tx_type = _tx_int(payload["type"], "type")
        if tx_type not in {0, 1, 2}:
            raise ValueError(f"unsupported transaction type: {tx_type}")
        if tx_type == 2 and not has_max_fee:
            raise ValueError("type 2 transaction requires EIP-1559 fees")
        if tx_type != 2 and not has_gas_price:
            raise ValueError(f"type {tx_type} transaction requires gasPrice")
        if tx_type == 0 and payload.get("accessList"):
            raise ValueError("legacy transaction cannot contain an accessList")
    _normalized_access_list(payload.get("accessList"))

def _normalized_address(value: Any) -> str | None:
    from web3 import Web3

    if value is None:
        return None
    raw = _hex_value(value)
    if raw in {"", "0x"}:
        return None
    return Web3.to_checksum_address(raw).lower()

def _normalized_data(value: Any) -> str:
    from hexbytes import HexBytes

    return "0x" + HexBytes(value if value is not None else b"").hex()

def _normalized_storage_key(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        number = int.from_bytes(value, "big")
    else:
        number = _tx_int(value, "accessList storage key")
    if number < 0 or number >= 2**256:
        raise ValueError("invalid accessList storage key")
    return f"0x{number:064x}"

def _normalized_access_list(value: Any) -> list[dict]:
    normalized = []
    for item in value or []:
        if isinstance(item, dict):
            address = item.get("address")
            storage_keys = item.get("storageKeys") or []
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            address, storage_keys = item
        else:
            raise ValueError("invalid accessList entry")
        normalized.append(
            {
                "address": _normalized_address(address),
                "storageKeys": [_normalized_storage_key(key) for key in storage_keys],
            }
        )
    return normalized

def _decode_signed_transaction(raw_tx: str) -> tuple[dict, str, str]:
    from eth_account import Account
    from eth_account._utils.legacy_transactions import Transaction
    from eth_account.typed_transactions import TypedTransaction
    from hexbytes import HexBytes
    from web3 import Web3

    raw = HexBytes(raw_tx)
    if not raw:
        raise ValueError("empty raw transaction")
    if raw[0] < 0x80:
        decoded = TypedTransaction.from_bytes(raw).as_dict()
    else:
        decoded = Transaction.from_bytes(raw).as_dict()
    recovered = Account.recover_transaction(raw)
    digest = "0x" + Web3.keccak(raw).hex()
    return decoded, recovered, digest

def _decoded_chain_id(decoded: dict) -> int:
    if decoded.get("chainId") is not None:
        return _tx_int(decoded["chainId"], "decoded chainId")
    value_v = _tx_int(decoded.get("v"), "decoded v")
    if value_v < 35:
        return 0
    return (value_v - 35) // 2

def _verify_claw_signed_result(payload: dict, result: dict, expected_from: str) -> dict:
    _validate_claw_payload(payload, expected_from)
    chain = _claw_chain_name(payload.get("chainId"))
    if str(result.get("chain") or "") != chain:
        raise ValueError("Claw result chain mismatch")
    decoded, recovered, computed_hash = _decode_signed_transaction(str(result.get("rawTx") or ""))

    expected_address = _normalized_address(expected_from)
    if not expected_address or _normalized_address(payload.get("from")) != expected_address:
        raise ValueError("transaction from does not match expected signer")
    if _normalized_address(result.get("from")) != expected_address:
        raise ValueError("Claw result from mismatch")
    if _normalized_address(recovered) != expected_address:
        raise ValueError("signed transaction recovered from mismatch")

    returned_hash = str(result.get("txHash") or "").lower()
    if returned_hash != computed_hash.lower():
        raise ValueError("signed transaction hash mismatch")
    if _decoded_chain_id(decoded) != _tx_int(payload.get("chainId"), "chainId"):
        raise ValueError("signed transaction chainId mismatch")
    if _normalized_address(decoded.get("to")) != _normalized_address(payload.get("to")):
        raise ValueError("signed transaction to mismatch")
    if _tx_int(decoded.get("value"), "decoded value") != _tx_int(payload.get("value", 0), "value"):
        raise ValueError("signed transaction value mismatch")
    if _normalized_data(decoded.get("data")) != _normalized_data(payload.get("data", "0x")):
        raise ValueError("signed transaction data mismatch")
    if _tx_int(decoded.get("nonce"), "decoded nonce") != _tx_int(payload.get("nonce"), "nonce"):
        raise ValueError("signed transaction nonce mismatch")
    if _tx_int(decoded.get("gas"), "decoded gas") != _tx_int(
        payload.get("gasLimit"), "gasLimit"
    ):
        raise ValueError("signed transaction gas mismatch")

    if payload.get("gasPrice") is not None:
        if _tx_int(decoded.get("gasPrice"), "decoded gasPrice") != _tx_int(
            payload["gasPrice"], "gasPrice"
        ):
            raise ValueError("signed transaction gasPrice mismatch")
        if decoded.get("maxFeePerGas") is not None or decoded.get("maxPriorityFeePerGas") is not None:
            raise ValueError("signed transaction fee model mismatch")
    else:
        if _tx_int(decoded.get("maxFeePerGas"), "decoded maxFeePerGas") != _tx_int(
            payload.get("maxFeePerGas"), "maxFeePerGas"
        ):
            raise ValueError("signed transaction maxFeePerGas mismatch")
        if _tx_int(
            decoded.get("maxPriorityFeePerGas"), "decoded maxPriorityFeePerGas"
        ) != _tx_int(payload.get("maxPriorityFeePerGas"), "maxPriorityFeePerGas"):
            raise ValueError("signed transaction maxPriorityFeePerGas mismatch")
        if decoded.get("gasPrice") is not None:
            raise ValueError("signed transaction fee model mismatch")

    if _normalized_access_list(decoded.get("accessList")) != _normalized_access_list(
        payload.get("accessList")
    ):
        raise ValueError("signed transaction accessList mismatch")
    decoded_type = _tx_int(decoded.get("type", 0), "decoded type")
    if payload.get("type") is not None:
        if decoded_type != _tx_int(payload["type"], "type"):
            raise ValueError("signed transaction type mismatch")
    elif payload.get("maxFeePerGas") is not None:
        if decoded_type != 2:
            raise ValueError("signed transaction inferred type mismatch")
    elif payload.get("accessList") is not None:
        if decoded_type != 1:
            raise ValueError("signed transaction inferred type mismatch")
    elif decoded_type not in {0, 1}:
        raise ValueError("signed transaction inferred type mismatch")

    return {
        "rawTx": str(result["rawTx"]),
        "txHash": computed_hash,
        "from": expected_from,
    }

def _sign_tx(env: dict, tx_dict: dict) -> dict:
    if env.get("signer") == "local":
        from eth_account import Account

        if not env.get("bsc_key"):
            raise PredictStatus("disabled")
        account = Account.from_key(env["bsc_key"])
        signed = account.sign_transaction(tx_dict)
        return {
            "rawTx": _hex_value(_signed_raw(signed)),
            "txHash": _hex_value(getattr(signed, "hash")),
            "from": account.address,
        }

    script = config.PKG_ROOT / "tagagent" / "tools" / "claw_sign.mjs"
    wallet_dir = str(env.get("claw_wallet_dir") or config.CLAW_WALLET_DIR)
    payload = _tx_payload(tx_dict)
    expected_from = _signer_address(env)
    try:
        _validate_claw_payload(payload, expected_from)
    except (TypeError, ValueError) as exc:
        raise PredictStatus("signer_unavailable", _sanitize_signer_error(str(exc)))
    try:
        proc = subprocess.run(
            ["node", str(script), "--wallet-dir", wallet_dir],
            input=json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            env={**os.environ, "TAGAI_PREDICT_RPC": str(env.get("rpc") or config.TAGAI_PREDICT_RPC)},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PredictStatus("signer_unavailable", _sanitize_signer_error(str(exc)))
    try:
        result = json.loads(proc.stdout.strip())
    except ValueError:
        result = {}
    error = str(result.get("error") or proc.stderr or "")
    if proc.returncode != 0 or not result.get("ok") or not result.get("rawTx") or not result.get("txHash"):
        raise PredictStatus("signer_unavailable", _sanitize_signer_error(error))
    try:
        return _verify_claw_signed_result(payload, result, expected_from)
    except (TypeError, ValueError) as exc:
        raise PredictStatus("signer_unavailable", _sanitize_signer_error(str(exc)))

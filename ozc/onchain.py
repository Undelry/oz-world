"""
oz_onchain.py — Read-only bridge to the real OZC SPL token on Solana.

This module exists so the OZ internal economy (oz_economy.py) can *observe*
the real on-chain world without being coupled to it. It is intentionally
minimal and side-effect-free:

  - No private keys, no signing, no sending.
  - No new pip dependencies — uses httpx which is already installed.
  - Self-contained: if this file is deleted, nothing else breaks.
    oz_economy.py imports it lazily and falls back cleanly.

Scope (Phase Alpha — read-only):
  - Query OZC total supply on-chain
  - Query an arbitrary wallet's OZC balance
  - Query an arbitrary wallet's native SOL balance
  - Simple in-process TTL cache so we don't hammer the public RPC

Out of scope (future phases, deliberately not implemented yet):
  - Sending OZC (needs a keypair + human approval)
  - Mint authority operations
  - Raydium pool interactions

The design goal is "reversible": wiring this in is a few lines in
oz_economy.py, and unwinding it is deleting those lines + this file.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

import httpx

# ================================
# Constants — OZC on Solana
# ================================
# The OZC SPL token mint. This is the actual on-chain identity of OZ Coin.
OZC_MINT = "AHZWRiVYmSw1Dr7y52GeJPwvo6Gwsbe5Y4t9fPWiis6F"

# SPL Token Program ID — constant for all SPL tokens on Solana.
SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# RPC endpoint. Override with OZ_SOLANA_RPC if you have a private endpoint
# (public one is rate-limited — fine for read-only monitoring).
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
RPC_URL = os.environ.get("OZ_SOLANA_RPC", DEFAULT_RPC)

# Path to the wallet directory file. Maps internal agent names to Solana
# wallet addresses. Optional — the bridge works without it for ad-hoc queries.
WALLETS_PATH = os.path.expanduser("~/.openclaw/workspace/oz_wallets.json")


# ================================
# Tiny TTL cache
# ================================
# The public RPC rate-limits aggressively. We cache for 30s by default since
# supply and balances don't move often for a freshly-launched token.
_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()
CACHE_TTL_SECONDS = 30.0


def _cache_get(key: str) -> Any | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.time() > expires_at:
            _cache.pop(key, None)
            return None
        return value


def _cache_put(key: str, value: Any, ttl: float = CACHE_TTL_SECONDS) -> None:
    with _cache_lock:
        _cache[key] = (time.time() + ttl, value)


def clear_cache() -> None:
    """Drop all cached RPC results. Call this if you need a fresh read."""
    with _cache_lock:
        _cache.clear()


# ================================
# JSON-RPC helpers
# ================================
class OnchainError(RuntimeError):
    """Raised when a Solana RPC call fails or returns unexpected data."""


def _rpc(method: str, params: list[Any], timeout: float = 10.0) -> Any:
    """Call a Solana JSON-RPC method and return the ``result`` field.

    Raises OnchainError on transport failure or RPC-level errors so callers
    can distinguish "network is down" from "the answer is zero".
    """
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        resp = httpx.post(RPC_URL, json=body, timeout=timeout)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise OnchainError(f"RPC transport error ({method}): {e}") from e

    try:
        data = resp.json()
    except json.JSONDecodeError as e:
        raise OnchainError(f"RPC non-JSON response ({method}): {e}") from e

    if "error" in data:
        raise OnchainError(f"RPC error ({method}): {data['error']}")
    if "result" not in data:
        raise OnchainError(f"RPC missing result ({method}): {data}")
    return data["result"]


# ================================
# Public API — read-only queries
# ================================
def get_ozc_total_supply() -> dict:
    """Return the total supply of OZC on-chain.

    Shape: {"amount_raw": int, "amount": float, "decimals": int}
    where ``amount`` is the human-readable supply (e.g. 100000000.0).
    """
    cached = _cache_get("ozc_supply")
    if cached is not None:
        return cached

    result = _rpc("getTokenSupply", [OZC_MINT])
    value = result.get("value", {})
    amount_raw = int(value.get("amount", "0"))
    decimals = int(value.get("decimals", 0))
    amount = amount_raw / (10 ** decimals) if decimals else float(amount_raw)
    out = {"amount_raw": amount_raw, "amount": amount, "decimals": decimals}
    _cache_put("ozc_supply", out)
    return out


def get_ozc_balance(wallet_address: str) -> dict:
    """Return the OZC balance of ``wallet_address``.

    Shape: {"amount_raw": int, "amount": float, "decimals": int,
            "token_accounts": int}

    ``amount`` is zero if the wallet holds no OZC token account. Multiple
    token accounts for the same mint are summed (unusual but possible).
    """
    key = f"ozc_bal:{wallet_address}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    result = _rpc(
        "getTokenAccountsByOwner",
        [
            wallet_address,
            {"mint": OZC_MINT},
            {"encoding": "jsonParsed"},
        ],
    )
    accounts = result.get("value", []) or []
    total_raw = 0
    decimals = 0
    for acc in accounts:
        info = (
            acc.get("account", {})
               .get("data", {})
               .get("parsed", {})
               .get("info", {})
               .get("tokenAmount", {})
        )
        total_raw += int(info.get("amount", "0"))
        decimals = int(info.get("decimals", decimals))
    amount = total_raw / (10 ** decimals) if decimals else float(total_raw)
    out = {
        "amount_raw": total_raw,
        "amount": amount,
        "decimals": decimals,
        "token_accounts": len(accounts),
    }
    _cache_put(key, out)
    return out


def get_sol_balance(wallet_address: str) -> float:
    """Return the native SOL balance of ``wallet_address`` in whole SOL."""
    key = f"sol_bal:{wallet_address}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    result = _rpc("getBalance", [wallet_address])
    lamports = int(result.get("value", 0))
    sol = lamports / 1_000_000_000
    _cache_put(key, sol)
    return sol


# ================================
# Wallet directory — agent → Solana address
# ================================
def load_wallets() -> dict:
    """Return the agent→address mapping.

    Schema (versioned so we can evolve it without breaking older files):
        {
          "version": 1,
          "wallets": {
            "hitomi":   {"address": "...", "role": "founder",  "notes": "..."},
            "treasury": {"address": "...", "role": "treasury", "notes": "..."}
          }
        }

    Returns the default skeleton if the file does not exist, so callers can
    always rely on a dict-shaped result.
    """
    if not os.path.isfile(WALLETS_PATH):
        return {"version": 1, "wallets": {}}
    try:
        with open(WALLETS_PATH) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "wallets": {}}
    if "wallets" not in data:
        data["wallets"] = {}
    return data


def get_agent_wallet(agent: str) -> str | None:
    """Return the Solana address mapped to ``agent``, or None if unmapped."""
    data = load_wallets()
    entry = data.get("wallets", {}).get(agent)
    if not entry:
        return None
    return entry.get("address")


def get_agent_onchain_balance(agent: str) -> dict | None:
    """Convenience: look up an agent's wallet and return its OZC balance.

    Returns None if the agent has no wallet mapping. Propagates OnchainError
    on RPC failure (callers choose whether to treat that as zero or as
    "unknown — retry later").
    """
    address = get_agent_wallet(agent)
    if not address:
        return None
    bal = get_ozc_balance(address)
    bal["address"] = address
    return bal


# ================================
# CLI — quick inspection from the terminal
# ================================
def _print_supply() -> None:
    s = get_ozc_total_supply()
    print(f"OZC total supply: {s['amount']:,.{s['decimals']}f} (raw: {s['amount_raw']})")


def _print_balance(wallet: str) -> None:
    b = get_ozc_balance(wallet)
    sol = get_sol_balance(wallet)
    print(f"Wallet: {wallet}")
    print(f"  OZC: {b['amount']:,.{b['decimals']}f} "
          f"(raw: {b['amount_raw']}, accounts: {b['token_accounts']})")
    print(f"  SOL: {sol:.6f}")


def _print_agents() -> None:
    data = load_wallets()
    wallets = data.get("wallets", {})
    if not wallets:
        print("(no agent wallets mapped — see oz_wallets.json)")
        return
    print(f"Loaded {len(wallets)} agent wallet(s) from {WALLETS_PATH}\n")
    for agent, info in wallets.items():
        addr = info.get("address", "?")
        role = info.get("role", "")
        try:
            bal = get_ozc_balance(addr)
            ozc = f"{bal['amount']:,.{bal['decimals']}f} OZC"
        except OnchainError as e:
            ozc = f"(rpc error: {e})"
        print(f"  {agent:12} [{role:10}] {addr}")
        print(f"               {ozc}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="OZC on-chain read-only CLI",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("supply", help="Show OZC total supply on Solana")

    bal_p = sub.add_parser("balance", help="Show OZC + SOL balance for a wallet")
    bal_p.add_argument("wallet", help="Solana wallet address (base58)")

    sub.add_parser("agents", help="Show balances of all mapped agent wallets")

    args = parser.parse_args()
    if args.cmd == "supply":
        _print_supply()
    elif args.cmd == "balance":
        _print_balance(args.wallet)
    elif args.cmd == "agents":
        _print_agents()


if __name__ == "__main__":
    main()

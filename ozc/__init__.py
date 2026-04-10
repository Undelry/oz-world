"""ozc — OZC settlement protocol for AI agent transactions.

A signed, hash-chained ledger with an HTTP API daemon and an optional
Solana on-chain bridge. The protocol is documented in OZC_PROTOCOL.md;
the reference implementation is contained entirely in this package and
the Python standard library.

Submodules:
    ozc.identity   — Ed25519 keypair, signing, verification
    ozc.ledger     — SQLite ledger, transfers, daemon, CLI core
    ozc.events     — signed event log (publish/list/verify)
    ozc.onchain    — read-only Solana SPL token bridge (optional)

Quick Python usage:
    from ozc import ledger, identity
    ledger.init_db()
    if not identity.has_identity():
        identity.init_identity()
    print(ledger.get_balance("hitomi"))
    tx = ledger.transfer("hitomi", "coder", 5, "agent_payment", "PR review")

Quick CLI usage:
    python3 -m ozc serve --port 8800
    python3 -m ozc balance hitomi
    python3 -m ozc transfer hitomi coder 5 --action agent_payment

See OZC_PROTOCOL.md for the full HTTP API and data-format specification.
"""

__version__ = "1.0.0"
__all__ = ["identity", "ledger", "events", "onchain", "__version__"]

# Submodules are NOT eagerly imported here. Pulling them lazily keeps
# `import ozc` cheap (no SQLite touch, no httpx import) and lets callers
# choose what they need:
#
#   from ozc import ledger          # touches SQLite
#   from ozc import identity        # imports cryptography lazily
#   from ozc import onchain         # imports httpx lazily
#
# Tools that want everything can do `from ozc import identity, ledger,
# events, onchain` themselves.

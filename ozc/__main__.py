"""Unified CLI entry point for the ozc package.

This is invoked as `python3 -m ozc <subcommand>`. After `pip install ozc`,
a `ozc` console script (defined in pyproject.toml) calls `main()` here.

Routing strategy
----------------
Each submodule (ledger, identity, events, onchain) already has its own
argparse-based ``main()``. Rather than duplicating every flag here, we
dispatch by *domain prefix*:

    ozc serve              → ledger.main()    (most common — bare alias)
    ozc balances           → ledger.main()
    ozc transfer ...       → ledger.main()
    ozc register ...       → ledger.main()
    ozc reputation ...     → ledger.main()
    ozc verify             → ledger.main()
    ozc onchain ...        → ledger.main()    (the integrated onchain cmd)

    ozc identity init      → identity.main()  (strips the "identity" prefix)
    ozc identity show      → identity.main()
    ozc identity sign ...  → identity.main()

    ozc events list        → events.main()
    ozc events publish ... → events.main()
    ozc events verify      → events.main()

    ozc on-supply          → onchain.main() supply
    ozc on-balance <wallet> → onchain.main() balance

This keeps the existing module CLIs intact (they still work as
``python3 -m ozc.ledger ...``) while presenting one unified surface.
"""

from __future__ import annotations

import sys
from typing import NoReturn


# Subcommand sets known to belong to each backing module. We compute the
# domain by checking the first positional arg against these sets, with
# explicit prefixes (`identity`, `events`) winning over the default
# `ledger` domain.
LEDGER_COMMANDS = {
    "init", "balances", "ledger", "stats", "reset", "verify", "onchain",
    "serve", "register", "reputation", "transfer", "charge",
}
IDENTITY_PREFIX = "identity"
EVENTS_PREFIX = "events"
ONCHAIN_PREFIXES = {"on-supply", "on-balance", "on-agents"}


def _print_help() -> None:
    print("ozc — OZC protocol CLI")
    print()
    print("Usage:")
    print("  python3 -m ozc <command> [args...]")
    print()
    print("Daemon:")
    print("  ozc serve [--port 8800] [--bind 127.0.0.1] [--no-auth]")
    print()
    print("Ledger / accounts:")
    print("  ozc init                            initialize the ledger DB")
    print("  ozc balances                        show all balances")
    print("  ozc ledger                          show recent transactions")
    print("  ozc stats                           today's spending stats")
    print("  ozc verify                          verify the chain integrity")
    print("  ozc transfer <from> <to> <amount>   send OZC")
    print("  ozc charge <agent> <action>         charge a known action")
    print("  ozc register <agent> [--initial N]  register a new agent")
    print("  ozc reputation <agent>              show agent reputation")
    print()
    print("Identity (Ed25519):")
    print("  ozc identity init                   generate keypair")
    print("  ozc identity show                   print public key")
    print("  ozc identity sign <data>            sign a string")
    print("  ozc identity verify <data> <sig> <pubkey>")
    print("  ozc identity sign-event <json>      sign a JSON event")
    print("  ozc identity verify-event <json>    verify a signed event")
    print()
    print("Signed events log:")
    print("  ozc events list                     list signed events")
    print("  ozc events publish <type> <json>    publish a new event")
    print("  ozc events verify                   verify all event signatures")
    print("  ozc events stats                    event log stats")
    print()
    print("On-chain bridge (Solana, read-only):")
    print("  ozc on-supply                       OZC total supply on Solana")
    print("  ozc on-balance <wallet>             OZC + SOL balance for a wallet")
    print("  ozc on-agents                       balances of mapped agent wallets")
    print()
    print("Or (also still supported as full module CLIs):")
    print("  python3 -m ozc.ledger ...")
    print("  python3 -m ozc.identity ...")
    print("  python3 -m ozc.events ...")
    print("  python3 -m ozc.onchain ...")
    print()
    print("See OZC_PROTOCOL.md for the HTTP API and data-format spec.")


def _dispatch_with_argv(target_main, argv: list[str]) -> None:
    """Run a backing module's main() with a temporary sys.argv override.

    The backing argparse uses sys.argv[0] for its program name (we set it
    to a friendly "ozc <domain>" form) and sys.argv[1:] for its commands.
    """
    saved = sys.argv
    try:
        sys.argv = argv
        target_main()
    finally:
        sys.argv = saved


def main() -> NoReturn:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        _print_help()
        raise SystemExit(0)

    head = args[0]

    # ----- Identity domain -----
    if head == IDENTITY_PREFIX:
        from ozc import identity as _identity
        # Strip the "identity" prefix and forward the rest. If the user just
        # ran `ozc identity` with no subcommand, show identity's own help.
        rest = args[1:] or ["--help"]
        _dispatch_with_argv(_identity.main, ["ozc identity", *rest])
        raise SystemExit(0)

    # ----- Events domain -----
    if head == EVENTS_PREFIX:
        from ozc import events as _events
        rest = args[1:] or ["--help"]
        _dispatch_with_argv(_events.main, ["ozc events", *rest])
        raise SystemExit(0)

    # ----- On-chain shortcuts -----
    # `ozc on-supply` / `on-balance` / `on-agents` map onto the underlying
    # `supply / balance / agents` commands of ozc.onchain.
    if head in ONCHAIN_PREFIXES:
        from ozc import onchain as _onchain
        sub = head[len("on-"):]  # "on-supply" → "supply"
        rest = args[1:]
        _dispatch_with_argv(_onchain.main, ["ozc on-chain", sub, *rest])
        raise SystemExit(0)

    # ----- Ledger domain (the default) -----
    if head in LEDGER_COMMANDS:
        from ozc import ledger as _ledger
        _dispatch_with_argv(_ledger.main, ["ozc", *args])
        raise SystemExit(0)

    # Unknown
    print(f"ozc: unknown command: {head}", file=sys.stderr)
    print("Try `ozc --help` for the full list of commands.", file=sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    main()

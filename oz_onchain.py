"""Compatibility shim — see ``ozc.onchain`` for the canonical module.

This file used to contain the read-only Solana SPL token bridge; it now
lives at ``ozc/onchain.py``. Existing code that does ``import oz_onchain``
keeps working unchanged: this shim re-aliases the old module name to point
at the package module so they share one object in ``sys.modules``.

For new code, prefer:

    from ozc import onchain
    onchain.get_ozc_total_supply()

For one-off command-line use:

    python3 -m ozc on-supply
    python3 -m ozc on-balance <wallet>
    python3 -m ozc on-agents
"""

from __future__ import annotations

import sys

from ozc import onchain as _real

if __name__ == "__main__":
    _real.main()
else:
    sys.modules[__name__] = _real

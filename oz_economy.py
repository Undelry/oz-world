"""Compatibility shim — see ``ozc.ledger`` for the canonical module.

This file used to contain the OZC ledger, transfer logic, daily caps,
HTTP API daemon, and CLI; it now lives at ``ozc/ledger.py``. Existing
code that does ``import oz_economy`` or ``from oz_economy import ...``
keeps working: this shim re-aliases the old module name to point at the
package module so the two are the same object in ``sys.modules``.

For new code, prefer:

    from ozc import ledger
    ledger.init_db()
    ledger.transfer("hitomi", "coder", 5, "agent_payment", "PR review")

For one-off command-line use:

    python3 -m ozc balances
    python3 -m ozc transfer hitomi coder 5
    python3 -m ozc serve --port 8800
"""

from __future__ import annotations

import sys

from ozc import ledger as _real

if __name__ == "__main__":
    # Direct script execution: `python3 oz_economy.py serve|balances|...`
    # Forward to the real module's CLI without touching sys.modules.
    _real.main()
else:
    sys.modules[__name__] = _real

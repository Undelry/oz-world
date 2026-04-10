"""Compatibility shim — see ``ozc.identity`` for the canonical module.

This file used to contain the Ed25519 identity implementation; it now lives
at ``ozc/identity.py``. Existing code that does ``import oz_identity`` or
``from oz_identity import ...`` keeps working: this shim re-aliases the
old module name to point at the package module so the two are the same
object in ``sys.modules``.

For new code, prefer:

    from ozc import identity
    identity.init_identity()

For one-off command-line use:

    python3 -m ozc identity init
    python3 -m ozc identity show
"""

from __future__ import annotations

import sys

from ozc import identity as _real

if __name__ == "__main__":
    # Direct script execution: `python3 oz_identity.py init|show|sign|...`
    # Forward to the real module's CLI without touching sys.modules.
    _real.main()
else:
    # Imported as a module: make `import oz_identity` literally return the
    # ozc.identity module so attribute access, mutation, and `from ... import`
    # all behave identically.
    sys.modules[__name__] = _real

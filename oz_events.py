"""Compatibility shim — see ``ozc.events`` for the canonical module.

This file used to contain the signed-event log; it now lives at
``ozc/events.py``. Existing code that does ``import oz_events`` keeps
working unchanged: this shim re-aliases the old module name to point at
the package module so they share one object in ``sys.modules``.

For new code, prefer:

    from ozc import events
    events.publish_event("place.publish", {"name": "Cafe", ...})

For one-off command-line use:

    python3 -m ozc events list
    python3 -m ozc events verify
"""

from __future__ import annotations

import sys

from ozc import events as _real

if __name__ == "__main__":
    _real.main()
else:
    sys.modules[__name__] = _real

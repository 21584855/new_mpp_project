"""Injectable clock.

All persisted timestamps are ISO 8601 UTC strings. Tests set ``MPPKB_NOW`` so
that TTL behaviour is deterministic.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone


def now() -> datetime:
    raw = os.environ.get("MPPKB_NOW", "").strip()
    if raw:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now().isoformat().replace("+00:00", "Z")

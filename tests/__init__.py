"""Tests for the Model Boss runtime."""

from __future__ import annotations

import sys
from pathlib import Path

# The runtime package lives inside the skill directory (boss-dispatch/runtime/),
# so make that directory importable before any test module imports runtime.*.
_SKILL_ROOT = Path(__file__).resolve().parents[1] / "boss-dispatch"
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

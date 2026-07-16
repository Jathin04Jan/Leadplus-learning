"""Put `src/` on the import path so scripts can `from intel import ...`.

`pyproject.toml` sets `package = false`, so there is no installed distribution to import from.
Every script imports this first.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

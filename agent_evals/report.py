"""JSON report generation for task runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class Report:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

"""Small JSON manifest recording stage progress, for resuming."""

import json
import os
from pathlib import Path
from typing import Any


class Manifest:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        tmp = self.path.with_suffix(".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(self._data, indent=2))
        os.replace(tmp, self.path)

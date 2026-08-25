from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml


@lru_cache(maxsize=1)
def load_config() -> dict:
    default_path = Path(__file__).resolve().parent.parent / "issue.yaml"
    path = Path(os.getenv("ISSUE_CONFIG_PATH", str(default_path)))
    if not path.exists():
        return {"issue_id": "v0", "injection": {"mode": "none", "parameters": {}}}
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    injection = value.get("injection") or {"mode": "none", "parameters": {}}
    return {"issue_id": value.get("issue_id", "v0"), "injection": injection}

"""Configuration loader. Reads configs/config.yaml into a dict-like object."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

# Project root = two levels up from this file (backend/core/config.py)
ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "configs" / "config.yaml"


class Config(dict):
    """Dict with attribute access and dotted-key lookup."""

    __getattr__ = dict.get

    def get_path(self, dotted: str, default=None):
        node = self
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def _wrap(obj):
    if isinstance(obj, dict):
        return Config({k: _wrap(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_wrap(v) for v in obj]
    return obj


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path) if path else CONFIG_PATH
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    cfg = _wrap(data)

    # Resolve and create runtime directories.
    for key in ("snapshot_dir", "recording_dir", "model_dir"):
        d = (ROOT / cfg.get_path(f"system.{key}", f"./{key}")).resolve()
        d.mkdir(parents=True, exist_ok=True)
        cfg["system"][key] = str(d)

    cfg["root"] = str(ROOT)
    return cfg


# Singleton — import `CONFIG` anywhere.
CONFIG = load_config()

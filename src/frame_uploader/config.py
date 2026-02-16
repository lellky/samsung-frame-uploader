"""Configuration management for Samsung Frame TV connection."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "frame-uploader" / "config.json"
DEFAULT_TOKEN_PATH = Path.home() / ".config" / "frame-uploader" / "tv-token.txt"

MATTE_TYPES = [
    "none",
    "modernthin",
    "modern",
    "modernwide",
    "flexible",
    "shadowbox",
    "panoramic",
    "triptych",
    "mix",
    "squares",
]

MATTE_COLORS = [
    "black",
    "neutral",
    "antique",
    "warm",
    "polar",
    "sand",
    "seafoam",
    "sage",
    "burgandy",
    "navy",
    "apricot",
    "byzantine",
    "lavender",
    "redorange",
    "skyblue",
    "turquoise",
]


@dataclass
class TVConfig:
    host: str = ""
    port: int = 8002
    token_file: str = str(DEFAULT_TOKEN_PATH)
    matte: str = "modern_warm"

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> TVConfig:
        if path.exists():
            data = json.loads(path.read_text())
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        return cls()

    def save(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n")

"""Centralize strategy choices and explicitly unverified image-only rules."""
from dataclasses import dataclass, field, fields
import json
from pathlib import Path


@dataclass
class Config:
    round_origin: int = 1
    # These three defaults are inferred from the upstream demo, NOT verified rules.
    layout_mode: str = "demo_inferred"
    weapon_cost: int = 25
    wall_stones: int = 1
    weapon_cells: list = field(default_factory=list)
    wall_cells: list = field(default_factory=list)
    loadout: list = field(default_factory=lambda: ["gatling", "railgun", "rocket"])
    stone_batch: int = 6
    sell_batch: int = 12
    return_margin: int = 5
    task_min_rounds: int = 8
    llm_enabled: bool = True
    daily_llm_limit: int = 3
    max_python_chars: int = 12000
    max_body_bytes: int = 2 * 1024 * 1024
    decision_seconds: float = 3.5
    build_retry_rounds: int = 20
    task_max_rounds: int = 40

    @classmethod
    def load(cls, path=None):
        if path is None:
            return cls()
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        unknown = set(raw) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown configuration keys: {sorted(unknown)}")
        cfg = cls(**raw)
        if cfg.layout_mode not in ("demo_inferred", "explicit"):
            raise ValueError("layout_mode must be demo_inferred or explicit")
        if not cfg.loadout or len(cfg.loadout) > 3 or any(
                x not in ("gatling", "railgun", "rocket") for x in cfg.loadout):
            raise ValueError("loadout must contain 1..3 weapons")
        for name in ("weapon_cost", "wall_stones", "stone_batch", "sell_batch",
                     "return_margin", "task_min_rounds", "max_body_bytes",
                     "build_retry_rounds", "task_max_rounds", "max_python_chars"):
            if type(getattr(cfg, name)) is not int or getattr(cfg, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not 0 < cfg.decision_seconds < 5 or cfg.round_origin not in (0, 1):
            raise ValueError("invalid deadline or round_origin")
        if type(cfg.daily_llm_limit) is not int or not 0 <= cfg.daily_llm_limit <= 3:
            raise ValueError("daily_llm_limit must be 0..3")
        for cell in cfg.weapon_cells + cfg.wall_cells:
            if not isinstance(cell, list) or len(cell) != 2 or any(type(v) is not int for v in cell):
                raise ValueError("explicit cells must be [x,y] integer pairs")
        return cfg

"""Compare worker logistics on the same deterministic finite-deposit maps."""
import argparse
import json
from pathlib import Path
import sys


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--multi-day", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(args.agent_root / "SDK/SDK_Python/CoreGeek"))
    from agent.brain import Agent
    from agent.config import Config
    from day_economy_benchmark import simulate
    cases = []
    if args.multi_day:
        for mirror in (False, True):
            cases.append(simulate(Agent, Config, profile="controlled", mirror=mirror, days=3, damage_walls=True))
    else:
        for case in ("near", "local_ore", "remote_ore", "far_shop"):
            for mirror in (False, True):
                cases.append(simulate(Agent, Config, profile="controlled", case=case, mirror=mirror))
    print(json.dumps({"simulation": "optimistic controlled fixtures; nearby cyclic deposits; no combat or task income", "cases": cases},
                     ensure_ascii=False, indent=2))

#!/usr/bin/env python3
"""Replay one request JSON or a JSON list of sequential turns. Not a game engine."""
import argparse
import json
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "SDK/SDK_Python/CoreGeek"))
from agent.brain import Agent
from agent.config import Config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request")
    parser.add_argument("--config")
    parser.add_argument("--output")
    parser.add_argument("--log-level", default="WARNING", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stderr, level=getattr(logging, args.log_level),
                        format="%(levelname)s %(name)s %(message)s")
    payload = json.loads(Path(args.request).read_text(encoding="utf-8"))
    agent = Agent(Config.load(args.config))
    result = [agent.decide(p) for p in payload] if isinstance(payload, list) else agent.decide(payload)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()

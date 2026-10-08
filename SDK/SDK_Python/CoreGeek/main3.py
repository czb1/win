#!/usr/bin/env python3
"""Competition entry point; callback() is also available for offline integration."""
import argparse
import logging
import os
from agent.logging_system import configure_logging
from agent.brain import Agent
from agent.config import Config
from agent.server import serve

_agent = None


def callback(json_data):
    global _agent
    if _agent is None:
        _agent = Agent(Config.load(os.environ.get("FUTURE_WAR_CONFIG")))
    return _agent.decide(json_data)


def main():
    parser = argparse.ArgumentParser(description="Future War Python agent")
    parser.add_argument("port", type=int)
    parser.add_argument("--config", default=os.environ.get("FUTURE_WAR_CONFIG"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    parser.add_argument("--log-dir", help="Save game.log and searchable events.jsonl")
    args = parser.parse_args()
    configure_logging(args.log_level, args.log_dir)
    cfg = Config.load(args.config)
    if cfg.layout_mode == "demo_inferred":
        logging.warning("Build layout/costs inferred from demo; confirm image-only rules before competition.")
    try:
        serve(args.port, cfg, args.host)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

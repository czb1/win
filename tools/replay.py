#!/usr/bin/env python3
"""Replay one request JSON or a JSON list of sequential turns. Not a game engine."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "SDK/SDK_Python/CoreGeek"))
from agent.logging_system import configure_logging
from agent.log_crypto import LogCryptoError
from agent.brain import Agent
from agent.config import Config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("request")
    parser.add_argument("--config")
    parser.add_argument("--output")
    parser.add_argument("--log-level", default="WARNING", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    parser.add_argument("--log-dir", help="Save game.log and searchable events.jsonl")
    parser.add_argument("--log-public-key", help="FWLOG public key; defaults to the agent's log-public.json")
    parser.add_argument("--plaintext-logs", action="store_true", help="Explicitly use legacy logs for local debugging")
    args = parser.parse_args()
    if args.plaintext_logs and args.log_public_key:
        parser.error("--plaintext-logs cannot be combined with --log-public-key")
    try:
        configure_logging(args.log_level, args.log_dir, args.log_public_key, args.plaintext_logs)
    except LogCryptoError:
        parser.exit(2, "日志加密公钥缺失或无效；请配置 --log-public-key，或仅本地调试时使用 --plaintext-logs。\n")
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


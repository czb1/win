#!/usr/bin/env bash
set -euo pipefail
agent_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec "${PYTHON:-python3}" "$agent_root/main3.py" "$@"

#!/usr/bin/env bash
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "Usage: $0 <PROTO> <DATA>" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [ -n "${PEACH_SDK_DIR:-}" ]; then
  case "$PEACH_SDK_DIR" in
    /*|~*) ;;
    *) export PEACH_SDK_DIR="$PWD/$PEACH_SDK_DIR" ;;
  esac
fi
exec uv run --project "$ROOT" --directory "$ROOT" python -m core.peach_runtime data "$@"

#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ] || [ $# -gt 3 ]; then
  echo "Usage: $0 <PROTO> [<SEED_DIR>] [<MUTATOR_FILTER>]" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [ -n "${PEACH_SDK_DIR:-}" ]; then
  case "$PEACH_SDK_DIR" in
    /*|~*) ;;
    *) export PEACH_SDK_DIR="$PWD/$PEACH_SDK_DIR" ;;
  esac
fi
PROTO=$(echo "$1" | tr '[:upper:]' '[:lower:]')
SEED_DIR="$ROOT/seeds/$PROTO"
if [ $# -ge 2 ]; then
  SEED_DIR="$(cd "$2" && pwd)"
fi
exec uv run --project "$ROOT" --directory "$ROOT" python -m core.peach_runtime mutator \
  "$PROTO" "$SEED_DIR" "${3:-}"

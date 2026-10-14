#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 <PROTO> [<SEED_DIR>] [<DATAMODEL_PATH>] [<DATAMODEL_NAME>] [<LOG_DIR>]" >&2
  exit 2
fi

PROTO=$(echo "$1" | tr '[:upper:]' '[:lower:]')
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [ -n "${PEACH_SDK_DIR:-}" ]; then
  case "$PEACH_SDK_DIR" in
    /*|~*) ;;
    *) export PEACH_SDK_DIR="$PWD/$PEACH_SDK_DIR" ;;
  esac
fi

SEED_DIR="$ROOT/seeds/$PROTO"

if [ $# -ge 2 ]; then
  if [ ! -d "$2" ]; then
    echo "Error: SEED_DIR '$2' does not exist or is not a directory." >&2
    exit 1
  fi
  SEED_DIR="$(cd "$2" && pwd)"
fi

DATAMODEL_PATH="${3:-$ROOT/llm/peach/$PROTO/datamodel.xml}"
DATAMODEL_NAME="${4:-${PROTO}_packet_array}"
LOG_DIR="${5:-$ROOT/llm/peach/$PROTO/dm_test_logs}"

if [ ! -f "$DATAMODEL_PATH" ]; then
  echo "Error: DATAMODEL_PATH '$DATAMODEL_PATH' does not exist or is not a file." >&2
  exit 1
fi

DATAMODEL_DIR="$(cd "$(dirname "$DATAMODEL_PATH")" && pwd)"
DATAMODEL_PATH="$DATAMODEL_DIR/$(basename "$DATAMODEL_PATH")"
DSL_ENTRY="$DATAMODEL_DIR/datamodel_dsl/root.py"
ERROR_REPORT="$DATAMODEL_DIR/datamodel_error_report.txt"

mkdir -p "$LOG_DIR"
LOG_DIR="$(cd "$LOG_DIR" && pwd)"
chmod u+rwx "$LOG_DIR"
find "$LOG_DIR" -maxdepth 1 -type f -name '*.log' -delete
rm -f "$ERROR_REPORT"

set +e
uv run --project "$ROOT" --directory "$ROOT" python -m core.peach_runtime datamodel \
  "$PROTO" "$SEED_DIR" "$DATAMODEL_PATH" "$DATAMODEL_NAME" "$LOG_DIR"
VALIDATOR_STATUS=$?
set -e

if compgen -G "$LOG_DIR/*.log" > /dev/null; then
  if [ ! -f "$DSL_ENTRY" ]; then
    echo "Error: DSL entry '$DSL_ENTRY' does not exist or is not a file." >&2
    exit 1
  fi
  uv run --project "$ROOT" --directory "$ROOT" python -m peach_dsl.error_report \
    --entry "$DSL_ENTRY" \
    --log-dir "$LOG_DIR" \
    --output "$ERROR_REPORT" \
    > /dev/null
fi

exit "$VALIDATOR_STATUS"

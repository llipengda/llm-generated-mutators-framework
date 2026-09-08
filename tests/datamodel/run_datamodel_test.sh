#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 <PROTO> [<SEED_DIR>] [<DATAMODEL_PATH>] [<DATAMODEL_NAME>] [<LOG_DIR>]" >&2
  exit 2
fi

PROTO=$(echo "$1" | tr '[:upper:]' '[:lower:]')
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$ROOT/tests/peach_sdk_env.sh"

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
DSL_ENTRY="$DATAMODEL_DIR/datamodel_dsl/root.py"
ERROR_REPORT="$DATAMODEL_DIR/datamodel_error_report.txt"

mkdir -p "$LOG_DIR"
chmod u+rwx "$LOG_DIR"
find "$LOG_DIR" -maxdepth 1 -type f -name '*.log' -delete
rm -f "$ERROR_REPORT"

CUSTOM_DLL="$ROOT/llm/peach/$PROTO/DataElements/out/$(echo "$PROTO" | tr '[:lower:]' '[:upper:]')DataElements.dll"
DOCKER_ARGS=(-v "$DATAMODEL_PATH:/test/datamodel.xml:ro" -v "$SEED_DIR:/seeds:ro" -v "$LOG_DIR:/logs")
PYTHON_FIXUP="$(dirname "$DATAMODEL_PATH")/python_fixup.py"
if [ -f "$PYTHON_FIXUP" ]; then
  DOCKER_ARGS+=(-v "$PYTHON_FIXUP:/generated/python_fixup.py:ro")
fi
if [ -f "$CUSTOM_DLL" ]; then
  DOCKER_ARGS+=(-v "$CUSTOM_DLL:/custom-data-elements.dll:ro")
fi

set +e
if [ "$PEACH_IS_MODERN" -eq 1 ]; then
  DOCKER_ARGS+=(-v "$ROOT/tests/NLog.config:/opt/peach/NLog.config:ro")
  docker run --rm -i --platform=linux/amd64 --entrypoint sh "${DOCKER_ARGS[@]}" "$PEACH_IMAGE" -c \
    'if [ -f /custom-data-elements.dll ]; then cp /custom-data-elements.dll /opt/peach/Plugins/; fi; exec dotnet /opt/peach/Peach.LLM.Validations.DataModel.dll /test/datamodel.xml "$1" /seeds' sh "$DATAMODEL_NAME"
else
  docker run --rm -i "${DOCKER_ARGS[@]}" "$PEACH_IMAGE" sh -c \
    'if [ -f /custom-data-elements.dll ]; then cp /custom-data-elements.dll ./Plugins/; fi; exec mono Peach.LLM.Validations.DataModel.exe /test/datamodel.xml "$1" /seeds' sh "$DATAMODEL_NAME"
fi
VALIDATOR_STATUS=$?
set -e

if compgen -G "$LOG_DIR/*.log" > /dev/null; then
  if [ ! -f "$DSL_ENTRY" ]; then
    echo "Error: DSL entry '$DSL_ENTRY' does not exist or is not a file." >&2
    exit 1
  fi
  uv run --project "$ROOT" python -m peach_dsl.error_report \
    --entry "$DSL_ENTRY" \
    --log-dir "$LOG_DIR" \
    --output "$ERROR_REPORT" \
    > /dev/null
fi

exit "$VALIDATOR_STATUS"

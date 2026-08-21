#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 <PROTO> [<SEED_DIR>] [<DATAMODEL_PATH>] [<DATAMODEL_NAME>] [<LOG_DIR>]" >&2
  exit 2
fi

PROTO=$(echo "$1" | tr '[:upper:]' '[:lower:]')
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

SEED_DIR="$ROOT/tests/seeds/$PROTO"

if [ $# -ge 2 ]; then
  if [ ! -d "$2" ]; then
    echo "Error: SEED_DIR '$2' does not exist or is not a directory." >&2
    exit 1
  fi
  SEED_DIR=$2
fi

DATAMODEL_PATH="${3:-$ROOT/llm/peach/$PROTO/datamodel.xml}"
DATAMODEL_NAME="${4:-${PROTO}_packet_array}"
LOG_DIR="${5:-$ROOT/llm/peach/$PROTO/dm_test_logs}"

if [ ! -f "$DATAMODEL_PATH" ]; then
  echo "Error: DATAMODEL_PATH '$DATAMODEL_PATH' does not exist or is not a file." >&2
  exit 1
fi

mkdir -p "$LOG_DIR"
chmod u+rwx "$LOG_DIR"

docker run --rm -i -v "$DATAMODEL_PATH:/test/datamodel.xml:ro" -v "$SEED_DIR:/seeds:ro" -v "$LOG_DIR:/logs" pdli/llm-peach:sdk \
  mono Peach.LLM.Validations.DataModel.exe /test/datamodel.xml "$DATAMODEL_NAME" /seeds

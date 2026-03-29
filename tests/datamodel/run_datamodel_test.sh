#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 <PROTO> [<SEED_DIR>]" >&2
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

docker run --rm --name datamodel_test -v "$ROOT/llm/peach/$PROTO:/test" -v "$SEED_DIR:/seeds" -v "$ROOT/llm/peach/$PROTO/dm_test_logs:/logs" peach \
  mono Peach.LLM.Validations.DataModel.exe /test/datamodel.xml $PROTO\_packet_array /seeds
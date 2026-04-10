#!/usr/bin/env bash
set -euo pipefail

if [ $# -ne 2 ]; then
    echo "Usage: $0 <PROTO> <DATA>"
    exit 1
fi

PROTO=$(echo "$1" | tr '[:upper:]' '[:lower:]')
DATA=$2

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

PIT_PATH="$ROOT/llm/peach/$PROTO/datamodel.xml"
DATAMODEL_NAME="${PROTO}_packet_array"

docker run --rm -v "$PIT_PATH":/datamodel.xml pdli/llm-peach:sdk \
    sh -c "mono Peach.LLM.Validations.Fixer.exe -d /datamodel.xml $DATAMODEL_NAME $DATA || (cat /logs/fixer.log && exit 1)" 2>&1
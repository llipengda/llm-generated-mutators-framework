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
PROTO_UPPER=$(echo "$PROTO" | tr '[:lower:]' '[:upper:]')
CUSTOM_DLL="$ROOT/llm/peach/$PROTO/DataElements/out/${PROTO_UPPER}DataElements.dll"
DOCKER_ARGS=(-v "$PIT_PATH:/datamodel.xml")
if [ -f "$CUSTOM_DLL" ]; then
    DOCKER_ARGS+=(-v "$CUSTOM_DLL:/custom-data-elements.dll:ro")
fi

docker run --rm "${DOCKER_ARGS[@]}" pdli/llm-peach:sdk \
    sh -c 'if [ -f /custom-data-elements.dll ]; then cp /custom-data-elements.dll ./Plugins/; fi; mono Peach.LLM.Validations.Fixer.exe -d /datamodel.xml "$1" "$2" || (cat /logs/fixer.log && exit 1)' sh "$DATAMODEL_NAME" "$DATA" 2>&1

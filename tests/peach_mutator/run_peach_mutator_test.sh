#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 <PROTO> [<SEED_DIR>] [<MUTATOR_FILTER>]" >&2
  echo "  MUTATOR_FILTER: comma-separated mutator names to test (optional)" >&2
  exit 2
fi

PROTO=$(echo "$1" | tr '[:upper:]' '[:lower:]')
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$ROOT/tests/peach_sdk_env.sh"
PROTO_UPPER=$(echo "$PROTO" | tr '[:lower:]' '[:upper:]')

SEED_DIR="$ROOT/seeds/$PROTO"

if [ $# -ge 2 ]; then
  if [ ! -d "$2" ]; then
    echo "Error: SEED_DIR '$2' does not exist or is not a directory." >&2
    exit 1
  fi
  SEED_DIR=$2
fi

MUTATOR_FILTER=""
if [ $# -ge 3 ]; then
  MUTATOR_FILTER=$3
fi

sed_i() {
  if [[ "$OSTYPE" == "darwin"* ]]; then
    sed -i '' "$@"
  else
    sed -i "$@"
  fi
}

COMPILE_ARGS=(--source-dir "$ROOT/llm/peach/$PROTO/Mutators" --output "$ROOT/llm/peach/$PROTO/Mutators/out/${PROTO_UPPER}Mutators.dll")
CUSTOM_DLL="$ROOT/llm/peach/$PROTO/DataElements/out/${PROTO_UPPER}DataElements.dll"
if [ -f "$CUSTOM_DLL" ]; then
  COMPILE_ARGS+=(--reference "$CUSTOM_DLL")
fi
uv run --project "$ROOT" python -m core.peach_sdk "${COMPILE_ARGS[@]}"

rm -rf "$ROOT/llm/peach/$PROTO/mutator_test_logs"
mkdir -p "$ROOT/llm/peach/$PROTO/mutator_test_logs"
chmod u+rwx "$ROOT/llm/peach/$PROTO/mutator_test_logs"

FILTER_ARG=""
if [ -n "$MUTATOR_FILTER" ]; then
  FILTER_ARG="$MUTATOR_FILTER"
fi

if [ "$PEACH_IS_MODERN" -eq 1 ]; then
  docker run --rm -i --platform=linux/amd64 --entrypoint sh -v "$ROOT/llm/peach/$PROTO":/generated -v "$SEED_DIR":/seeds \
    -v "$ROOT/tests/NLog.config:/opt/peach/NLog.config:ro" \
    -v "$ROOT/llm/peach/$PROTO/mutator_test_logs:/logs" "$PEACH_IMAGE" \
    -c "cp /generated/Mutators/out/${PROTO_UPPER}Mutators.dll /opt/peach/Plugins && \
    if [ -f /generated/DataElements/out/${PROTO_UPPER}DataElements.dll ]; then cp /generated/DataElements/out/${PROTO_UPPER}DataElements.dll /opt/peach/Plugins; fi && \
    if [ -f /generated/python_fixup.py ]; then cp /generated/python_fixup.py /opt/peach/; fi && \
    dotnet /opt/peach/Peach.LLM.Validations.Mutator.dll /generated/datamodel.xml /seeds ${PROTO}_packet_array 100 ${FILTER_ARG} && \
    chmod -R 777 /logs"
else
  docker run --rm -i -v "$ROOT/llm/peach/$PROTO":/generated -v "$SEED_DIR":/seeds \
    -v "$ROOT/llm/peach/$PROTO/mutator_test_logs:/logs" "$PEACH_IMAGE" \
    sh -c "cp /generated/Mutators/out/${PROTO_UPPER}Mutators.dll ./Plugins && \
    if [ -f /generated/DataElements/out/${PROTO_UPPER}DataElements.dll ]; then cp /generated/DataElements/out/${PROTO_UPPER}DataElements.dll ./Plugins; fi && \
    if [ -f /generated/python_fixup.py ]; then cp /generated/python_fixup.py ./; fi && \
    mono Peach.LLM.Validations.Mutator.exe /generated/datamodel.xml /seeds ${PROTO}_packet_array 100 ${FILTER_ARG} && \
    chmod -R 777 /logs"
fi

find "$ROOT/llm/peach/$PROTO/mutator_test_logs" -type f -print0 | while IFS= read -r -d '' log_file; do
  sed_i "s|/generated|$ROOT/llm/peach/$PROTO|g" "$log_file"
  sed_i "s|/seeds|$SEED_DIR|g" "$log_file"
done

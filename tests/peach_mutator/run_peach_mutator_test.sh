#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 <PROTO> [<SEED_DIR>] [<MUTATOR_FILTER>]" >&2
  echo "  MUTATOR_FILTER: comma-separated mutator names to test (optional)" >&2
  exit 2
fi

PROTO=$(echo "$1" | tr '[:upper:]' '[:lower:]')
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROTO_UPPER=$(echo "$PROTO" | tr '[:lower:]' '[:upper:]')

SEED_DIR="$ROOT/tests/seeds/$PROTO"

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

mcs -sdk:4.5 "$ROOT/llm/peach/$PROTO/Mutators/*.cs" \
    $(printf -- '-r:%s ' $ROOT/peach/sdk/*.dll) \
    -target:library \
    -warnaserror \
    -out:"$ROOT/llm/peach/$PROTO/Mutators/out/${PROTO_UPPER}Mutators.dll"

rm -rf "$ROOT/llm/peach/$PROTO/mutator_test_logs"
mkdir -p "$ROOT/llm/peach/$PROTO/mutator_test_logs"
chmod u+rwx "$ROOT/llm/peach/$PROTO/mutator_test_logs"

FILTER_ARG=""
if [ -n "$MUTATOR_FILTER" ]; then
  FILTER_ARG="$MUTATOR_FILTER"
fi

docker run --rm -i -v "$ROOT/llm/peach/$PROTO":/generated -v "$SEED_DIR":/seeds \
    -v "$ROOT/llm/peach/$PROTO/mutator_test_logs:/logs" pdli/llm-peach:sdk \
    sh -c "cp /generated/Mutators/out/${PROTO_UPPER}Mutators.dll ./Plugins && \
    mono Peach.LLM.Validations.Mutator.exe /generated/datamodel.xml /seeds ${PROTO}_packet_array 100 ${FILTER_ARG}"

find "$ROOT/llm/peach/$PROTO/mutator_test_logs" -type f -print0 | while IFS= read -r -d '' log_file; do
  sed_i "s|/generated|$ROOT/llm/peach/$PROTO|g" "$log_file"
  sed_i "s|/seeds|$SEED_DIR|g" "$log_file"
done

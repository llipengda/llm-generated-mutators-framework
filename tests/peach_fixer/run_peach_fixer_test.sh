#!/usr/bin/env bash
set -euo pipefail
set -x

if [ $# -lt 1 ]; then
  echo "Usage: $0 <PROTO>" >&2
  exit 2
fi

PROTO=$(echo "$1" | tr '[:upper:]' '[:lower:]')
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROTO_UPPER=$(echo "$PROTO" | tr '[:lower:]' '[:upper:]')

sed_i() {
  if [[ "$OSTYPE" == "darwin"* ]]; then
    sed -i '' "$@"
  else
    sed -i "$@"
  fi
}

if [ -f "$ROOT/peach/sdk/${PROTO_UPPER}Fixers.dll" ]; then
    rm -f "$ROOT/peach/sdk/${PROTO_UPPER}Fixers.dll"
fi

mcs -sdk:4.5 "$ROOT/llm/peach/$PROTO/Fixers/*.cs" \
    "$ROOT/llm/peach/$PROTO/Fixers/Validations/*.cs" \
    $(printf -- '-r:%s ' $ROOT/peach/sdk/*.dll) \
    -target:library \
    -warnaserror \
    -out:"$ROOT/llm/peach/$PROTO/Fixers/Validations/out/${PROTO_UPPER}FixerTests.dll"

rm -rf "$ROOT/llm/peach/$PROTO/fixer_test_logs"
mkdir -p "$ROOT/llm/peach/$PROTO/fixer_test_logs"
chmod u+rwx "$ROOT/llm/peach/$PROTO/fixer_test_logs"

docker run --rm -it -v "$ROOT/llm/peach/$PROTO":/generated \
    -v "$ROOT/llm/peach/$PROTO/fixer_test_logs:/logs" pdli/llm-peach:sdk \
    sh -c "cp /generated/Fixers/Validations/out/${PROTO_UPPER}FixerTests.dll ./Plugins && \
    cp /generated/datamodel.xml ./ && \
    mono Peach.LLM.Validations.Fixer.exe"

find "$ROOT/llm/peach/$PROTO/fixer_test_logs" -type f -print0 | while IFS= read -r -d '' log_file; do
  sed_i "s|/generated|$ROOT/llm/peach/$PROTO|g" "$log_file"
done

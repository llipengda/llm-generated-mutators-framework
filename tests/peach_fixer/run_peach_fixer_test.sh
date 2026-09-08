#!/usr/bin/env bash
set -euo pipefail
set -x

if [ $# -lt 1 ]; then
  echo "Usage: $0 <PROTO>" >&2
  exit 2
fi

PROTO=$(echo "$1" | tr '[:upper:]' '[:lower:]')
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$ROOT/tests/peach_sdk_env.sh"
PROTO_UPPER=$(echo "$PROTO" | tr '[:lower:]' '[:upper:]')

sed_i() {
  if [[ "$OSTYPE" == "darwin"* ]]; then
    sed -i '' "$@"
  else
    sed -i "$@"
  fi
}

COMPILE_ARGS=(
  --source-dir "$ROOT/llm/peach/$PROTO/Fixers"
  --source-dir "$ROOT/llm/peach/$PROTO/Fixers/Validations"
  --output "$ROOT/llm/peach/$PROTO/Fixers/Validations/out/${PROTO_UPPER}FixerTests.dll"
  --exclude-reference-name "${PROTO_UPPER}Fixers.dll"
)
CUSTOM_DLL="$ROOT/llm/peach/$PROTO/DataElements/out/${PROTO_UPPER}DataElements.dll"
if [ -f "$CUSTOM_DLL" ]; then
  COMPILE_ARGS+=(--reference "$CUSTOM_DLL")
fi
uv run --project "$ROOT" python -m core.peach_sdk "${COMPILE_ARGS[@]}"

rm -rf "$ROOT/llm/peach/$PROTO/fixer_test_logs"
mkdir -p "$ROOT/llm/peach/$PROTO/fixer_test_logs"
chmod u+rwx "$ROOT/llm/peach/$PROTO/fixer_test_logs"

if [ "$PEACH_IS_MODERN" -eq 1 ]; then
  docker run --rm -i --platform=linux/amd64 --entrypoint sh -v "$ROOT/llm/peach/$PROTO":/generated \
    -v "$ROOT/tests/NLog.config:/opt/peach/NLog.config:ro" \
    -v "$ROOT/llm/peach/$PROTO/fixer_test_logs:/logs" "$PEACH_IMAGE" \
    -c "cp /generated/Fixers/Validations/out/${PROTO_UPPER}FixerTests.dll /opt/peach/Plugins && \
    if [ -f /generated/DataElements/out/${PROTO_UPPER}DataElements.dll ]; then cp /generated/DataElements/out/${PROTO_UPPER}DataElements.dll /opt/peach/Plugins; fi && \
    cp /generated/datamodel.xml /opt/peach/ && \
    if [ -f /generated/python_fixup.py ]; then cp /generated/python_fixup.py /opt/peach/; fi && \
    cd /opt/peach && dotnet Peach.LLM.Validations.Fixer.dll"
else
  docker run --rm -i -v "$ROOT/llm/peach/$PROTO":/generated \
    -v "$ROOT/llm/peach/$PROTO/fixer_test_logs:/logs" "$PEACH_IMAGE" \
    sh -c "cp /generated/Fixers/Validations/out/${PROTO_UPPER}FixerTests.dll ./Plugins && \
    if [ -f /generated/DataElements/out/${PROTO_UPPER}DataElements.dll ]; then cp /generated/DataElements/out/${PROTO_UPPER}DataElements.dll ./Plugins; fi && \
    cp /generated/datamodel.xml ./ && \
    if [ -f /generated/python_fixup.py ]; then cp /generated/python_fixup.py ./; fi && \
    mono Peach.LLM.Validations.Fixer.exe"
fi

find "$ROOT/llm/peach/$PROTO/fixer_test_logs" -type f -print0 | while IFS= read -r -d '' log_file; do
  sed_i "s|/generated|$ROOT/llm/peach/$PROTO|g" "$log_file"
done

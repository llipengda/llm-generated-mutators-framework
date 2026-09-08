#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ] || { [ "$#" -eq 2 ] && [ "$2" != "--modern-sdk" ]; }; then
  echo "Usage: $0 <PROTO> [--modern-sdk]" >&2
  exit 2
fi

PROTO=$(echo "$1" | tr '[:upper:]' '[:lower:]')
PROTO_UPPER=$(echo "$PROTO" | tr '[:lower:]' '[:upper:]')
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GENERATED="$ROOT/llm/peach/$PROTO"
SDK_VARIANT="${PEACH_SDK:-legacy}"
if [ "${2:-}" = "--modern-sdk" ]; then
  SDK_VARIANT="modern"
fi

if [ ! -d "$GENERATED" ]; then
  echo "Error: generated protocol directory '$GENERATED' does not exist." >&2
  exit 1
fi

write_pit() {
  local strategy=$1
  local destination=$2
  cat > "$destination" <<EOF
<?xml version="1.0" encoding="utf-8"?>
<Peach xmlns="http://peachfuzzer.com/2012/Peach">
  <Include ns="dm" src="file:datamodel.xml" />
  <StateModel name="TheState" initialState="Initial">
    <State name="Initial"><Action type="output">
      <DataModel ref="dm:${PROTO}_packet_array"/><Data fileName="/seed/*.raw" />
    </Action></State>
  </StateModel>
  <Test name="Default">
    <StateModel ref="TheState"/>
    <Publisher class="TcpClient">
      <Param name="Host" value="##HOST##"/><Param name="Port" value="##PORT##"/>
      <Param name="Timeout" value="200"/>
    </Publisher>
    $strategy
    <Logger class="File"><Param name="Path" value="/logs"/></Logger>
  </Test>
</Peach>
EOF
}

write_pit '<Strategy class="TwoPhaseRandom"><Param name="TwoPhaseMutation" value="True"/><Param name="MultipleMutationsPerElement" value="3"/></Strategy>' "$GENERATED/pit.llm.xml"
write_pit '<Strategy class="Random"/>' "$GENERATED/pit.peach.xml"

if [ "$SDK_VARIANT" = "modern" ] || [ "$SDK_VARIANT" = "modern-sdk" ]; then
  BASE_IMAGE="pdli/llm-peach:modern-sdk"
  WORKDIR="/opt/peach"
  RUNNER="dotnet Peach.dll"
  PLATFORM="--platform=linux/amd64"
elif [ "$SDK_VARIANT" = "legacy" ] || [ "$SDK_VARIANT" = "sdk" ]; then
  BASE_IMAGE="pdli/llm-peach:sdk"
  WORKDIR="/peach/output/linux_x86_64_release/bin"
  RUNNER="./peach"
  PLATFORM=""
else
  echo "Error: PEACH_SDK must be 'legacy' or 'modern'." >&2
  exit 2
fi

for flavor in llm peach; do
  dockerfile="$GENERATED/Dockerfile.$flavor"
  plugin_copy=""
  if [ "$flavor" = "llm" ]; then
    plugin_copy="COPY Mutators/out/${PROTO_UPPER}Mutators.dll ${WORKDIR}/Plugins/"
  fi
  cat > "$dockerfile" <<EOF
FROM $PLATFORM $BASE_IMAGE
WORKDIR $WORKDIR
ENV HOST= PORT= PEACH_ARGS=
COPY datamodel.xml pit.$flavor.xml ./
$plugin_copy
ENTRYPOINT []
CMD ["bash", "-c", "sed -i 's/##HOST##/'\"\${HOST}\"'/g; s/##PORT##/'\"\${PORT}\"'/g' pit.$flavor.xml && $RUNNER pit.$flavor.xml \${PEACH_ARGS}"]
EOF
  docker build -t "pdli/llm-peach:${PROTO}-${flavor}" -f "$dockerfile" "$GENERATED"
done

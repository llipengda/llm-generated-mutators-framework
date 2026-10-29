#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ] || [[ "$1" == *[!a-zA-Z0-9_-]* ]]; then
  echo "Usage: $0 <protocol>" >&2
  exit 2
fi
PROTO="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
PROTO_UPPER="$(printf '%s' "$PROTO" | tr '[:lower:]' '[:upper:]')"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GENERATED="$ROOT/llm/peach/$PROTO"
SDK_DIR="${PEACH_SDK_DIR:-$ROOT/peach/sdk}"
if [ ! -f "$GENERATED/datamodel.xml" ]; then
  echo "Error: generated DataModel '$GENERATED/datamodel.xml' does not exist." >&2
  exit 1
fi
if [ ! -f "$SDK_DIR/Peach.runtimeconfig.json" ]; then
  echo "Error: local Peach runtime is missing; run ./setup.sh first." >&2
  exit 1
fi
SDK_DIR="$(cd "$SDK_DIR" && pwd)"
export PEACH_SDK_DIR="$SDK_DIR"

cd "$ROOT"
uv run --project "$ROOT" python -m core.mutator_codegen compile "$PROTO" \
  --output "$GENERATED/Mutators/out/${PROTO_UPPER}Mutators.dll"

write_pit() {
  local strategy=$1
  local destination=$2
  cat > "$destination" <<EOF
<?xml version="1.0" encoding="utf-8"?>
<Peach xmlns="http://peachfuzzer.com/2012/Peach">
  <Include ns="dm" src="file:datamodel.xml" />
  <StateModel name="TheState" initialState="Initial">
    <State name="Initial"><Action type="output">
      <DataModel ref="dm:${PROTO}_packet_array"/><Data fileName="##SEED_DIR##/*.raw" />
    </Action></State>
  </StateModel>
  <Test name="Default">
    <StateModel ref="TheState"/>
    <Publisher class="TcpClient">
      <Param name="Host" value="##HOST##"/><Param name="Port" value="##PORT##"/>
      <Param name="Timeout" value="200"/>
    </Publisher>
    $strategy
    <Logger class="File"><Param name="Path" value="##LOG_DIR##"/></Logger>
  </Test>
</Peach>
EOF
}

mkdir -p "$GENERATED/native"
for flavor in llm peach; do
  STAGING="$(mktemp -d "$GENERATED/native/.$flavor.XXXXXX")"
  cp -R "$SDK_DIR/." "$STAGING/"
  cp "$GENERATED/datamodel.xml" "$STAGING/"
  if [ -f "$GENERATED/python_fixup.py" ]; then
    cp "$GENERATED/python_fixup.py" "$STAGING/"
  fi
  for directory in "$STAGING" "$STAGING/Plugins"; do
    for assembly in "$directory/"*.dll; do
      case "${assembly##*/}" in
        *Mutators.dll|*Fixers.dll|*FixerTests.dll|*DataElements.dll)
          [ ! -f "$assembly" ] || rm "$assembly"
          ;;
      esac
    done
  done
  # Start with the SDK's two shared extensions; generated protocol plugins
  # belong only to this bundle and never enter the shared SDK installation.
  PLUGINS="$STAGING/ProtocolPlugins"
  mkdir -p "$PLUGINS"
  cp "$SDK_DIR/Peach.LLM.dll" "$SDK_DIR/Peach.LLM.Validations.Common.dll" "$PLUGINS/"
  assembly="$GENERATED/DataElements/out/${PROTO_UPPER}DataElements.dll"
  [ ! -f "$assembly" ] || cp "$assembly" "$PLUGINS/"
  if [ "$flavor" = llm ]; then
    cp "$GENERATED/Mutators/out/${PROTO_UPPER}Mutators.dll" "$PLUGINS/"
    strategy='<Strategy class="TwoPhaseRandom"><Param name="TwoPhaseMutation" value="True"/><Param name="MultipleMutationsPerElement" value="3"/></Strategy>'
  else
    strategy='<Strategy class="Random"/>'
  fi
  write_pit "$strategy" "$STAGING/pit.xml"
  cp "$STAGING/pit.xml" "$GENERATED/pit.$flavor.xml"
  mkdir -p "$STAGING/seeds"
  if [ -d "$ROOT/seeds/$PROTO" ]; then
    cp -R "$ROOT/seeds/$PROTO/." "$STAGING/seeds/"
  fi
  cat > "$STAGING/run.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
BUNDLE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="${HOST:-127.0.0.1}"
: "${PORT:?Set PORT to the target TCP port}"
SEED_DIR="${SEED_DIR:-$BUNDLE/seeds}"
LOG_DIR="${LOG_DIR:-$BUNDLE/Logs}"
if [ ! -d "$SEED_DIR" ]; then
  echo "Error: seed directory '$SEED_DIR' does not exist." >&2
  exit 1
fi
SEED_DIR="$(cd "$SEED_DIR" && pwd)"
mkdir -p "$LOG_DIR"
LOG_DIR="$(cd "$LOG_DIR" && pwd)"
xml_escape() {
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g'
}
cd "$BUNDLE"
exec dotnet "$BUNDLE/Peach.dll" --noweb --plugins="$BUNDLE/ProtocolPlugins" \
  -D "HOST=$(xml_escape "$HOST")" -D "PORT=$(xml_escape "$PORT")" \
  -D "SEED_DIR=$(xml_escape "$SEED_DIR")" -D "LOG_DIR=$(xml_escape "$LOG_DIR")" \
  "$@" "$BUNDLE/pit.xml"
EOF
  chmod +x "$STAGING/run.sh"
  DESTINATION="$GENERATED/native/$flavor"
  if [ -e "$DESTINATION" ]; then
    mv "$DESTINATION" "$DESTINATION.backup.$(date +%Y%m%d%H%M%S).$$"
  fi
  mv "$STAGING" "$DESTINATION"
  echo "Created native $flavor bundle: $DESTINATION"
done
echo "Run with: HOST=127.0.0.1 PORT=<target-port> $GENERATED/native/llm/run.sh [Peach options]"

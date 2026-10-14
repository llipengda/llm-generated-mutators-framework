#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="${PEACH_SOURCE_DIR:-$ROOT/../llm-peach}"
SDK_DIR="${PEACH_SDK_DIR:-$ROOT/peach/sdk}"

if [ "$#" -eq 2 ] && [ "$1" = "--source" ]; then
  SOURCE_DIR="$2"
elif [ "$#" -ne 0 ]; then
  echo "Usage: $0 [--source <llm-peach-directory>]" >&2
  exit 2
fi

command -v dotnet >/dev/null 2>&1 || {
  echo "Error: install a .NET 8 SDK or later before running setup." >&2
  exit 1
}
if ! dotnet --list-sdks | awk -F. '$1 >= 8 { found=1 } END { exit !found }'; then
  echo "Error: a .NET 8 SDK or later is required." >&2
  exit 1
fi
command -v xmllint >/dev/null 2>&1 || {
  echo "Error: install xmllint (libxml2) before running setup." >&2
  exit 1
}
if [ ! -d "$SOURCE_DIR" ]; then
  echo "Error: llm-peach source directory '$SOURCE_DIR' does not exist." >&2
  exit 1
fi
SOURCE_DIR="$(cd "$SOURCE_DIR" && pwd)"
SDK_PARENT="$(dirname "$SDK_DIR")"
mkdir -p "$SDK_PARENT"
SDK_PARENT="$(cd "$SDK_PARENT" && pwd)"
SDK_DIR="$SDK_PARENT/$(basename "$SDK_DIR")"
STAGING="$(mktemp -d "$SDK_PARENT/.sdk-install.XXXXXX")"
trap 'if [ -n "${STAGING:-}" ]; then echo "Incomplete setup files preserved in: $STAGING" >&2; fi' EXIT

# Publish only runtime projects. The aggregate build also includes test apps
# and optional instrumentation tools, which are unnecessary for this pipeline.
PROJECTS=(
  pro/Peach/Peach.csproj
  pro/PitTool/PitTool.csproj
  llm/Validations/DataModel/Peach.LLM.Validations.DataModel.csproj
  llm/Validations/Mutator/Peach.LLM.Validations.Mutator.csproj
  llm/Validations/Fixer/Peach.LLM.Validations.Fixer.csproj
)
for project in "${PROJECTS[@]}"; do
  if [ ! -f "$SOURCE_DIR/$project" ]; then
    echo "Error: missing .NET project '$SOURCE_DIR/$project'." >&2
    exit 1
  fi
  dotnet publish "$SOURCE_DIR/$project" --configuration Release --framework net8.0 \
    --output "$STAGING" --nologo -p:PeachInstallTargetFramework=net8.0
done

mkdir -p "$STAGING/Plugins"
cp "$STAGING/Peach.LLM.dll" "$STAGING/Peach.LLM.Validations.Common.dll" "$STAGING/Plugins/"
for launcher in peach pittool; do
  assembly=Peach
  [ "$launcher" != pittool ] || assembly=PitTool
  cat > "$STAGING/$launcher" <<EOF
#!/usr/bin/env bash
set -euo pipefail
LAUNCHER_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
exec dotnet "\$LAUNCHER_DIR/$assembly.dll" "\$@"
EOF
  chmod +x "$STAGING/$launcher"
done

# Probe the published distribution before replacing any existing installation.
(
  cd "$STAGING"
  ./peach --showenv > peach.txt
  ./pittool makexsd
)
for assembly in Peach PitTool Peach.LLM.Validations.DataModel Peach.LLM.Validations.Mutator Peach.LLM.Validations.Fixer; do
  for suffix in dll deps.json runtimeconfig.json; do
    if [ ! -f "$STAGING/$assembly.$suffix" ]; then
      echo "Error: published installation is missing '$assembly.$suffix'." >&2
      exit 1
    fi
  done
done
if [ -e "$SDK_DIR" ]; then
  BACKUP="$SDK_DIR.backup.$(date +%Y%m%d%H%M%S).$$"
  mv "$SDK_DIR" "$BACKUP"
  echo "Previous SDK preserved in: $BACKUP"
fi
mv "$STAGING" "$SDK_DIR"
STAGING=""
mkdir -p "$ROOT/peach"
cp "$SDK_DIR/peach.txt" "$SDK_DIR/peach.xsd" "$ROOT/peach/"
cp "$ROOT/docs/llm-peach-sdk.md" "$ROOT/peach/README.md"
echo "Installed local .NET 8 Peach runtime and SDK in: $SDK_DIR"

#!/bin/bash
set -euo pipefail

if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "--modern-sdk" ]; }; then
    echo "Usage: $0 [--modern-sdk]"
    exit 1
fi

SDK_VARIANT="legacy"
IMAGE="pdli/llm-peach:sdk"
SDK_DIR="peach/sdk"
if [ "${1:-}" = "--modern-sdk" ]; then
    SDK_VARIANT="modern"
    IMAGE="pdli/llm-peach:modern-sdk"
    SDK_DIR="peach/modern-sdk"
fi
PLATFORM_ARG=""
if [ "$SDK_VARIANT" = "legacy" ]; then
    PLATFORM_ARG="--platform=linux/amd64"
fi

OS=$(uname -s)
ARCH=$(uname -m)

if ! command -v docker &> /dev/null
then
    echo "Docker could not be found, please install it first."
    exit 1
fi
if [ "$SDK_VARIANT" = "legacy" ]; then
    if ! command -v mono &> /dev/null; then
        echo "Mono could not be found, please install it first."
        exit 1
    fi
elif ! command -v dotnet &> /dev/null; then
    echo ".NET SDK could not be found, please install it first."
    exit 1
fi
if [[ "$SDK_VARIANT" == "legacy" && "$OS" == "Linux" && "$ARCH" == "aarch64" ]]; then
    echo "Linux arm64 is not supported."
    exit 1
fi


if ! docker pull "$IMAGE" $PLATFORM_ARG; then
    if docker image inspect "$IMAGE" >/dev/null 2>&1; then
        echo "Warning: Docker pull failed; using the existing local $IMAGE image." >&2
    else
        echo "Error: Docker pull failed and $IMAGE is not available locally." >&2
        exit 1
    fi
fi
if [ "$SDK_VARIANT" != "legacy" ]; then
    if ! dotnet --list-sdks | awk -F. '$1 >= 8 { found=1 } END { exit !found }'; then
        echo "Error: .NET 8 SDK or later is required for --modern-sdk."
        exit 1
    fi
fi
ESSENTIAL_DLLS=(
    BouncyCastle.Crypto.dll
    Dapper.dll
    Microsoft.Diagnostics.Runtime.dll
    Microsoft.Scripting.dll
    Newtonsoft.Json.dll
    NLog.dll
    Patterns.Logging.dll
    Peach.Core.dll
    Peach.LLM.dll
    Peach.Pro.dll
    Peach.LLM.Validations.Common.dll
    SocketHttpListener.dll
    vtortola.WebSockets.dll
    nunit.framework.dll
)
mkdir -p "$SDK_DIR"
chmod -R 777 peach
if [ "$SDK_VARIANT" = "legacy" ]; then
    docker run --rm -v "$(pwd)/peach:/p" "$IMAGE" $PLATFORM_ARG \
        sh -c "cp /peach/output/linux_x86_64_release/bin/${ESSENTIAL_DLLS[*]} /p/sdk/ \
               && /peach/output/linux_x86_64_release/bin/peach --showenv > /p/peach.txt \
               && cp /peach/llm/Core/README.md /p/README.md \
               && /peach/output/linux_x86_64_release/bin/pittool makexsd \
               && cp /peach/output/linux_x86_64_release/bin/peach.xsd /p/peach.xsd"
else
    docker run --rm --entrypoint sh -v "$(pwd)/peach:/p" "$IMAGE" $PLATFORM_ARG \
        -c "cd /opt/peach && cp *.dll /p/modern-sdk/ \
            && /opt/peach/Peach --showenv > /p/peach.txt \
            && cd /p && /opt/peach/PitTool makexsd"
fi

printf '%s\n' "$SDK_VARIANT" > "$SDK_DIR/variant"

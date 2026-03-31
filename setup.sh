#!/bin/bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "Usage: $0 [aflnet|peach]"
    exit 1
fi

TARGET=$1

OS=$(uname -s)
ARCH=$(uname -m)

if ! command -v gcc &> /dev/null
then
    echo "gcc could not be found, please install it first."
    exit 1
fi

case "$TARGET" in
    "aflnet")
        echo "Done."
        ;;
    "peach")
        if ! command -v docker &> /dev/null
        then
            echo "Docker could not be found, please install it first."
            exit 1
        fi
        if ! command -v mono &> /dev/null
        then
            echo "Mono could not be found, please install it first."
            exit 1
        fi
        if [[ "$OS" == "Darwin" && "$ARCH" == "arm64" ]]; then
            if ! command -v colima &> /dev/null
            then
                echo "Colima could not be found, please install it first."
                exit 1
            fi
            colima start --arch x86_64
        fi
        if [[ "$OS" == "Linux" && "$ARCH" == "aarch64" ]]; then
            echo "Linux arm64 is not supported."
            exit 1
        fi
        docker pull pdli/llm-peach:sdk
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
            SocketHttpListener.dll
            vtortola.WebSockets.dll
        )
        mkdir -p peach/sdk
        chmod 777 peach
        chmod 777 peach/sdk
        docker run --rm -v "$(pwd)/peach:/p" pdli/llm-peach:sdk \
            sh -c "cp /peach/output/linux_x86_64_release/bin/${ESSENTIAL_DLLS[*]} /p/sdk/ \
                   && /peach/output/linux_x86_64_release/bin/peach --showenv > /p/peach.txt \
                   && cp /peach/llm/Core/README.md /p/README.md"
        python3 process_peach_txt.py
        ;;
    *)
        echo "Unknown target: $TARGET"
        echo "Usage: $0 [aflnet|peach]"
        exit 1
        ;;
esac
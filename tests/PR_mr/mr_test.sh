#!/bin/bash

set -x

PROTO=$1
SEED_DIR=$2

SEED_DIR=$(realpath "$SEED_DIR")

if [ -z "$PROTO" ] || [ -z "$SEED_DIR" ]; then
  echo "Usage: $0 <PROTO> <SEED_DIR>"
  exit 1
fi

cd "$(dirname "$0")" || exit 1

LLM_DIR="../../llm/$PROTO"

find . -name '*.gcda' -delete >/dev/null 2>&1 || true

gcc -g mr_test.c $LLM_DIR/"${PROTO}_parser.c" $LLM_DIR/"${PROTO}_reassembler.c" $LLM_DIR/"${PROTO}_packets.c" -o mr_test -I"$LLM_DIR" -I. -DPROTO="$PROTO" -Wall -Wextra -fsanitize=address -fprofile-arcs

./mr_test "$SEED_DIR"
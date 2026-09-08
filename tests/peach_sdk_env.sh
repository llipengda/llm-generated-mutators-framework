#!/usr/bin/env bash

case "${PEACH_SDK:-legacy}" in
  legacy|sdk)
    PEACH_IMAGE="pdli/llm-peach:sdk"
    PEACH_IS_MODERN=0
    ;;
  modern|modern-sdk)
    PEACH_IMAGE="pdli/llm-peach:modern-sdk"
    PEACH_IS_MODERN=1
    ;;
  *)
    echo "Error: PEACH_SDK must be 'legacy' or 'modern'." >&2
    exit 2
    ;;
esac

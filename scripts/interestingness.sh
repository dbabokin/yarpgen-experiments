#!/usr/bin/env bash
# Interestingness test for creduce or cvise.
# A candidate is interesting only when:
#   1. ASan + UBSan at -O0 accept it (the UB-free oracle), and
#   2. the original failure is still present (crash, or checksum mismatch).
#
# Run with the case directory as cwd. Configure the failure via the environment:
#   YARPGEN_LANG          c or cxx
#   YARPGEN_KIND          ice or miscompile
#   YARPGEN_BAD_CC        compiler that crashes or disagrees
#   YARPGEN_BAD_FLAGS     extra flags, default -O3
#   YARPGEN_REF_CC        reference compiler (miscompile only)
#   YARPGEN_REF_FLAGS     default -O0
set -euo pipefail
lang="${YARPGEN_LANG:-c}"
kind="${YARPGEN_KIND:-miscompile}"
if [[ "$lang" == "cxx" ]]; then
  bad="${YARPGEN_BAD_CC:-g++}"
  ref="${YARPGEN_REF_CC:-g++}"
  srcs=(func.cpp driver.cpp)
  std=(-std=c++17)
else
  bad="${YARPGEN_BAD_CC:-gcc}"
  ref="${YARPGEN_REF_CC:-gcc}"
  srcs=(func.c driver.c)
  std=(-std=c11)
fi
bad_flags=(${YARPGEN_BAD_FLAGS:--O3})
ref_flags=(${YARPGEN_REF_FLAGS:--O0})
common=(-fsigned-char -Wno-unknown-pragmas "${std[@]}")

san_cc="${YARPGEN_SAN_CC:-$ref}"
if ! "$san_cc" "${common[@]}" -O0 -Werror=uninitialized \
    -fsanitize=undefined,address -fno-sanitize-recover=undefined,address \
    -o /tmp/yarpgen-interest-san "${srcs[@]}" > /tmp/yarpgen-interest-san.log 2>&1; then
  exit 1
fi
export ASAN_OPTIONS=detect_leaks=0:halt_on_error=1
export UBSAN_OPTIONS=halt_on_error=1
if ! /tmp/yarpgen-interest-san > /tmp/yarpgen-interest-san.out 2> /tmp/yarpgen-interest-san.err; then
  exit 1
fi
if grep -Eiq 'runtime error:|AddressSanitizer|UndefinedBehaviorSanitizer' /tmp/yarpgen-interest-san.err; then
  exit 1
fi

if [[ "$kind" == "ice" ]]; then
  if "$bad" "${common[@]}" "${bad_flags[@]}" -o /tmp/yarpgen-interest-bad "${srcs[@]}" \
      > /tmp/yarpgen-interest-bad.log 2>&1; then
    exit 1
  fi
  if grep -Eiq 'internal compiler error|please submit a bug report|please attach the following|stack dump:' \
      /tmp/yarpgen-interest-bad.log; then
    exit 0
  fi
  exit 1
fi

"$ref" "${common[@]}" "${ref_flags[@]}" -o /tmp/yarpgen-interest-ref "${srcs[@]}"
"$bad" "${common[@]}" "${bad_flags[@]}" -o /tmp/yarpgen-interest-bad "${srcs[@]}"
ref_out=$(/tmp/yarpgen-interest-ref)
bad_out=$(/tmp/yarpgen-interest-bad)
[[ "$ref_out" != "$bad_out" ]]

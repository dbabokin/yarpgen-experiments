#!/usr/bin/env bash
# Build GCC and/or LLVM from source into /opt. Invoked by Dockerfile.toolchains.
set -euo pipefail

GCC_VERSION="${GCC_VERSION:-14.2.0}"
LLVM_VERSION="${LLVM_VERSION:-18.1.8}"
BUILD_GCC="${BUILD_GCC:-1}"
BUILD_LLVM="${BUILD_LLVM:-1}"
JOBS="${JOBS:-$(nproc)}"

apt-get update
apt-get install -y --no-install-recommends \
  build-essential ca-certificates cmake ninja-build python3 \
  wget xz-utils git flex bison texinfo \
  libgmp-dev libmpfr-dev libmpc-dev zlib1g-dev libzstd-dev
rm -rf /var/lib/apt/lists/*

mkdir -p /tmp/src /opt

if [[ "$BUILD_GCC" == "1" ]]; then
  wget -O /tmp/gcc.tar.xz "https://ftp.gnu.org/gnu/gcc/gcc-${GCC_VERSION}/gcc-${GCC_VERSION}.tar.xz"
  tar -C /tmp/src -xf /tmp/gcc.tar.xz
  mkdir -p /tmp/gcc-build
  cd /tmp/gcc-build
  "/tmp/src/gcc-${GCC_VERSION}/configure" \
    --prefix=/opt/gcc \
    --enable-languages=c,c++ \
    --disable-multilib \
    --disable-bootstrap \
    --disable-nls
  make -j"$JOBS"
  make install
  cd /
  rm -rf /tmp/gcc-build "/tmp/src/gcc-${GCC_VERSION}" /tmp/gcc.tar.xz
fi

if [[ "$BUILD_LLVM" == "1" ]]; then
  wget -O /tmp/llvm.tar.xz \
    "https://github.com/llvm/llvm-project/releases/download/llvmorg-${LLVM_VERSION}/llvm-project-${LLVM_VERSION}.src.tar.xz"
  tar -C /tmp/src -xf /tmp/llvm.tar.xz
  cmake -G Ninja -S "/tmp/src/llvm-project-${LLVM_VERSION}.src/llvm" -B /tmp/llvm-build \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=/opt/llvm \
    -DLLVM_ENABLE_PROJECTS=clang \
    -DLLVM_TARGETS_TO_BUILD=X86 \
    -DLLVM_ENABLE_ASSERTIONS=ON \
    -DLLVM_INCLUDE_TESTS=OFF \
    -DLLVM_INCLUDE_BENCHMARKS=OFF \
    -DCLANG_ENABLE_ARCMT=OFF
  ninja -C /tmp/llvm-build -j"$JOBS" install
  rm -rf /tmp/llvm-build "/tmp/src/llvm-project-${LLVM_VERSION}.src" /tmp/llvm.tar.xz
fi

python3 - <<'PY'
import json
from pathlib import Path
compilers = []
if Path("/opt/gcc/bin/gcc").exists():
    compilers.append({"name": "gcc-O0", "cc": "/opt/gcc/bin/gcc", "cxx": "/opt/gcc/bin/g++", "flags": ["-O0"]})
    compilers.append({"name": "gcc-O3", "cc": "/opt/gcc/bin/gcc", "cxx": "/opt/gcc/bin/g++", "flags": ["-O3"]})
if Path("/opt/llvm/bin/clang").exists():
    compilers.append({"name": "clang-O0", "cc": "/opt/llvm/bin/clang", "cxx": "/opt/llvm/bin/clang++", "flags": ["-O0"]})
    compilers.append({"name": "clang-O3", "cc": "/opt/llvm/bin/clang", "cxx": "/opt/llvm/bin/clang++", "flags": ["-O3"]})
Path("/opt/yarpgen-compilers.json").write_text(json.dumps({"compilers": compilers}, indent=2) + "\n")
print("wrote /opt/yarpgen-compilers.json")
PY

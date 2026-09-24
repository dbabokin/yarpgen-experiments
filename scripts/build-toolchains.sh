#!/usr/bin/env bash
# Build the from-source GCC/LLVM image. See docker/Dockerfile.toolchains.
set -euo pipefail
cd "$(dirname "$0")/.."
gcc_version="${GCC_VERSION:-14.2.0}"
llvm_version="${LLVM_VERSION:-18.1.8}"
jobs="${JOBS:-4}"
build_gcc="${BUILD_GCC:-1}"
build_llvm="${BUILD_LLVM:-1}"
tag="${YARPGEN_TOOLCHAIN_TAG:-yarpgen-toolchains}"
docker build -f docker/Dockerfile.toolchains \
  --build-arg "GCC_VERSION=${gcc_version}" \
  --build-arg "LLVM_VERSION=${llvm_version}" \
  --build-arg "JOBS=${jobs}" \
  --build-arg "BUILD_GCC=${build_gcc}" \
  --build-arg "BUILD_LLVM=${build_llvm}" \
  -t "$tag" .
echo "image ${tag} ready. Compilers: /opt/gcc/bin and /opt/llvm/bin"
echo "config fragment: /opt/yarpgen-compilers.json"

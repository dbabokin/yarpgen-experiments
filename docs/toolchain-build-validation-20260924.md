# Toolchain build validation — 2026-09-24

**Result: both single-compiler paths validated.** Each official entrypoint finished as its own image. A combined GCC+LLVM image (`BUILD_GCC=1 BUILD_LLVM=1` in one build) was not run.

GCC-only, first: `BUILD_LLVM=0 JOBS=4 bash scripts/build-toolchains.sh` exited 0. That image was `sha256:e24f5e0dc596` (about 2.0 GiB) with GCC 14.2.0 under `/opt/gcc`. A one-test campaign was `tested=1 ok=1 failures=0`.

LLVM-only, later the same day: `BUILD_GCC=0 JOBS=4 bash scripts/build-toolchains.sh` also exited 0. Details are in the LLVM section below. The default tag `yarpgen-toolchains:latest` now points at the LLVM image because the script always tags that name. The GCC image id above is unchanged.

## Static checks

| Check | Result |
| --- | --- |
| `bash -n scripts/build-toolchains.sh` | pass |
| `bash -n docker/build-inside.sh` | pass |
| `shellcheck` 0.9.0 on both scripts | pass, no findings |
| Dockerfile parse | pass: `docker build` ran all 15 steps |
| ARG/ENV wiring | pass: build fetched `gcc-14.2.0` and configure used `--prefix=/opt/gcc` |
| GCC tarball HEAD | pass: `https://ftp.gnu.org/gnu/gcc/gcc-14.2.0/gcc-14.2.0.tar.xz` HTTP 200, `application/x-xz`, 92,306,460 bytes |
| LLVM tarball HEAD | pass: `llvmorg-18.1.8/llvm-project-18.1.8.src.tar.xz` HTTP 302 then 200, 132,067,260 bytes |

Host `awk` before the fix was mawk 1.3.4. There was no `gawk` binary.

## Bugs fixed before the successful build

These are in commit `207aa50`.

- `docker/build-inside.sh` did not install GNU awk. GCC's release build runs `gawk` (`opt-gather.awk` and others). Ubuntu 24.04's `/usr/bin/awk` is mawk. The script now installs `gawk`. The build log shows `checking for gawk... gawk` and later `gawk -f .../gcc/opt-gather.awk`.
- Configure now passes `--enable-host-pie`. Ubuntu 24.04's host GCC defaults to PIE. The build log shows `PICFLAG=-fPIE` and `prefix=/opt/gcc`.
- The image did not register `/opt/gcc/lib64` with the dynamic linker, so a C++ program linked against the new `libstdc++` could not start. `ldconfig` writes `/etc/ld.so.conf.d/yarpgen-toolchains.conf`, and the Dockerfile sets `LD_LIBRARY_PATH`. After the build, `ldd` on a `g++` binary resolved `libstdc++.so.6` and `libgcc_s.so.1` to `/opt/gcc/lib64`.
- `docker/Dockerfile.toolchains` set `WORKDIR /opt/yarpgen` and `CMD ["python3", "-m", "yarpgen.campaign", "--help"]` without copying the tree. `docker/Dockerfile.campaign` does copy `yarpgen`, `campaign`, and `scripts`. The toolchain image now copies those same directories after the compiler layer. `python3 -m yarpgen.campaign --help` runs with no bind mount.
- `DEBIAN_FRONTEND=noninteractive` is set so `apt-get` does not stop on a config prompt.

`docker/Dockerfile.campaign` is unchanged. It is still the distro `gcc`/`clang` worker and `campaign/example-config.json`. The toolchain image is the from-source worker: same tree, compilers from `/opt/yarpgen-compilers.json`.

## What actually ran

First attempt, default Docker overlay snapshotter, 2026-09-24T19:46:27Z to 19:46:29Z, exit 1:

```
failed to mount ... fstype: overlay ... err: invalid argument
```

This VM's root is already overlay, and the kernel rejected a nested overlay mount. That is an environment limit, not a failure of the Dockerfile. Dockerd was restarted with `storage-driver: fuse-overlayfs` and the containerd snapshotter disabled. A `docker run --rm ubuntu:24.04 echo container-ok` then passed.

Successful build:

```
BUILD_LLVM=0 JOBS=4 bash scripts/build-toolchains.sh
```

| | |
| --- | --- |
| Start | 2026-09-24T19:47:22Z |
| End | 2026-09-24T19:57:56Z |
| Wall time | 10m 34s |
| Exit | 0 |
| Docker | 29.1.3, storage driver `fuse-overlayfs`, legacy builder (buildx is not installed; Docker printed the deprecation warning and continued) |
| Image | `yarpgen-toolchains:latest`, id `sha256:e24f5e0dc596c75135dd9a0063299e071a1dd9baa51cd0cc43ef68e4033613dd`, size 2,189,301,566 bytes |

The log shows the tarball fetch, GNU awk in use, install of the target libs, and:

```
wrote /opt/yarpgen-compilers.json
Successfully tagged yarpgen-toolchains:latest
image yarpgen-toolchains ready. Compilers: /opt/gcc/bin and /opt/llvm/bin
```

`/opt/llvm/bin` is on `PATH` but empty. `clang` is absent, which matches `BUILD_LLVM=0`.

## Installed compiler

```
gcc (GCC) 14.2.0
g++ (GCC) 14.2.0
/opt/gcc/bin/gcc
/opt/gcc/bin/g++
```

Tiny programs (`int main` returning 0), compiled `-O2` and executed in the image: C exit 0, C++ exit 0. The C++ binary loads `/opt/gcc/lib64/libstdc++.so.6`.

`/opt/yarpgen-compilers.json` matches the disk:

```
gcc-O0 cc /opt/gcc/bin/gcc executable=True
gcc-O0 cxx /opt/gcc/bin/g++ executable=True
gcc-O3 cc /opt/gcc/bin/gcc executable=True
gcc-O3 cxx /opt/gcc/bin/g++ executable=True
clang_present False
match
```

Campaign entrypoint, no source mount:

```
python3 -m yarpgen.campaign --help
python3 -m yarpgen.campaign --config /opt/yarpgen-compilers.json --tests 1 --profile tiny --lang both --out /tmp/camp --seed 7
done tested=1 ok=1 failures=0 store=/tmp/camp
```

## Caveats

- A single image containing both `/opt/gcc` and `/opt/llvm` was not built. The default `scripts/build-toolchains.sh` (both compilers, `JOBS` default 4) is still unrun. Each compiler was built by skipping the other.
- `lto-plugin/configure` printed `/usr/bin/file: No such file or directory`. The build continued, and `/opt/gcc/libexec/gcc/x86_64-pc-linux-gnu/14.2.0/liblto_plugin.so` is present. Campaigns in this tree do not enable LTO. The `file` package is not installed in the image.
- On a normal host, Docker's overlay driver is enough. This nested VM needed `fuse-overlayfs`.
- The legacy builder warning is from Docker 29 without the buildx plugin. The build completed with that builder.

## LLVM-only build

Same daemon as the GCC run: Docker 29.1.3, `storage-driver: fuse-overlayfs`, containerd snapshotter off. No LLVM-specific script change was required. `docker/build-inside.sh` configured and installed Clang as written.

```
BUILD_GCC=0 JOBS=4 bash scripts/build-toolchains.sh
```

| | |
| --- | --- |
| Start | 2026-09-24T20:05:02Z |
| End | 2026-09-24T20:44:10Z |
| Wall time | 39m 8s |
| Exit | 0 |
| Image | `yarpgen-toolchains:latest`, id `sha256:068dc5a015e349c60f62d297a95febb8d3752e548b8ccb26e8b71f26b8a9dfe0`, size 3,521,991,954 bytes |
| Configure | Clang 18.1.8, Ninja, `CMAKE_INSTALL_PREFIX=/opt/llvm`, `LLVM_ENABLE_PROJECTS=clang`, `LLVM_TARGETS_TO_BUILD=X86`, `LLVM_ENABLE_ASSERTIONS=ON`, tests and benchmarks off |
| Ninja | 3722 steps, then install |

The log records `Clang version: 18.1.8`, `Build files have been written to: /tmp/llvm-build`, `wrote /opt/yarpgen-compilers.json`, and `Successfully tagged yarpgen-toolchains:latest`. `/opt/gcc/bin/gcc` is absent. `/usr/bin/gcc` is still the distro compiler from `build-essential`, which the image uses to compile LLVM. It is not listed in the compilers fragment.

### Installed Clang

```
clang version 18.1.8
Target: x86_64-unknown-linux-gnu
InstalledDir: /opt/llvm/bin
```

`clang++` prints the same version and install dir. `command -v` resolves both to `/opt/llvm/bin/clang` and `/opt/llvm/bin/clang++`.

Tiny programs (`int main` returning 0), compiled `-O2` and executed in the image: C exit 0, C++ exit 0. The C binary links only libc. The C++ binary loads the distro `/lib/x86_64-linux-gnu/libstdc++.so.6`, which is expected when `/opt/gcc` was not built.

`/opt/yarpgen-compilers.json` lists only the Clang entries, and both paths are executable:

```
names ['clang-O0', 'clang-O3']
clang-O0 cc /opt/llvm/bin/clang executable=True
clang-O0 cxx /opt/llvm/bin/clang++ executable=True
clang-O3 cc /opt/llvm/bin/clang executable=True
clang-O3 cxx /opt/llvm/bin/clang++ executable=True
gcc_present False
only_clang True
match
```

One campaign test, no source mount, that config:

```
python3 -m yarpgen.campaign --config /opt/yarpgen-compilers.json --tests 1 --profile tiny --lang both --out /tmp/camp --seed 7
done tested=1 ok=1 failures=0 store=/tmp/camp
```

The wrapper's closing line still says `Compilers: /opt/gcc/bin and /opt/llvm/bin` even when GCC was skipped. The install itself did not create `/opt/gcc`.

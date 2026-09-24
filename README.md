# YARPGen (reimplementation)

Generative fuzzer for C and C++ compilers. This repository started as the
YARPGen white papers; the generator and campaign stack here are a new
implementation of those designs, not a copy of the original source tree.

The papers this follows are in `papares/`:

- Livinskii, Babokin, Regehr. *Random Testing for C and C++ Compilers with YARPGen*. OOPSLA 2020.
- Livinskii, Babokin, Regehr. *Fuzzing Loop Optimizations in Compilers for C++ and Data-Parallel Languages*. PLDI 2023.
- Livinskii. *Better Generative Compiler Fuzzing for Unsafe Languages*. PhD thesis, University of Utah, 2024.

## What a run does

```text
generate program  ->  compile with several compilers and -O levels
        |                      |
        |                      +-- crash or checksum mismatch?
        |                                 |
        v                                 v
 checksum of outputs              store the case, then reduce it
 (successes are deleted)          only if ASan/UBSan still accept it
```

Generated programs are free of undefined behavior by construction. Sanitizers
are the oracle that checks the generator and that guards test reduction.
They are not used to filter a stream of programs that were allowed to be
undefined in the first place.

## Design

**Undefined behavior is rewritten locally.** Every expression is type-checked
and evaluated on concrete values while it is built. If an operation would be
undefined for those values (signed overflow, division by zero, a bad shift),
it is replaced by a nearby defined operation, following Table 1 of the OOPSLA
paper. Subexpressions are already safe, so the rewrite does not search.

**Inputs, mixed variables, and outputs are separate.** Inputs are initialized
in the driver and never written. Outputs are initialized to zero and only
written. Mixed globals may be updated, and their values are tracked. The
function under test is compiled in its own translation unit, so the compiler
cannot see the concrete inputs (separate compilation; do not enable LTO).

**The driver prints one checksum.** Any correct compiler, at any optimization
level, must print the same integer. The generator also knows that integer,
because it tracked the values, and the smoke test compares the two. The
check is not emitted as an assertion inside the program: a hard-coded
expected value fights automated reduction (PLDI 2023, test oracles).

**Generation policies skew the random choices.** Each test shuffles its own
distributions (types, operators, statement kinds, constant styles, leaf
kinds). Regions of an expression are restricted to an operator context:
additive, bitwise, logical, multiplicative, bitwise-shift, or
additive-multiplicative. Constants are often small, extreme, or bit-blocks,
and a buffer reuses earlier constants and their negations and complements.
A common-subexpression buffer pastes a previous pure expression back into
the tree so CSE and value numbering have something to find.

**Loops are planned, then filled in.** The skeleton decides the iteration
space and the idiom before expressions are generated:

| Kind | What it is there to trigger |
| --- | --- |
| uniform | vectorizable loop, same value in every input element |
| partition | even/odd values, UB analysis run twice, split hidden behind an opaque zero |
| stencil | several constant offsets from one induction variable |
| reduction | unsigned accumulator (`+`, `^`, `&`, `|`) |
| byte | copy or fill of an `unsigned char` buffer (memcpy/memset idioms) |
| nest | perfect 2-deep nest, row-major or column-major |
| fusion | two adjacent loops that share a trip count |

Induction variables are not used as ordinary arithmetic leaves. Their ranges
are chosen so the header itself cannot overflow, including the increment that
exits the loop. Trip-count bounds are often opaque `const` inputs: the
compiler cannot see the value, and every iteration still sees inputs the
generator has already checked. That is the PLDI approach (uniform values, or
an even/odd partition) rather than unrolling every iteration or wrapping
every operation in a dynamic check.

**One choice sequence per seed.** Every random draw is logged. Replacing a
single draw and replaying the seed mutates the program while the rest of the
stream stays aligned (thesis chapter 4). Coverage-guided search over that
sequence is not implemented; the hook is there so a later campaign can do it.

## Layout

| Path | Role |
| --- | --- |
| `yarpgen/ub.py` | local UB rewrites |
| `yarpgen/policy.py` | shuffled distributions and operator contexts |
| `yarpgen/generate.py` | skeleton, expressions, loops |
| `yarpgen/lower.py` | C and C++ header, function, driver |
| `yarpgen/campaign.py` | compile matrix, failure store, reduction |
| `yarpgen/reduce.py` | line reducer with a sanitizer oracle |
| `scripts/interestingness.sh` | same oracle for creduce or cvise |
| `docker/Dockerfile.toolchains` | GCC and LLVM built from source |
| `docker/Dockerfile.campaign` | campaign image using distro compilers |
| `scripts/campaign-ssh.sh` | rsync + ssh launch |

Assumptions, all documented so a disagreement is a compiler bug rather than
an ABI mismatch: LP64, two's complement, `int` 32-bit, `long` and
`long long` 64-bit. The build always passes `-fsigned-char`. Generated code
uses `signed char` and `unsigned char` explicitly anyway.

## Generate a program

No install step. From the repository root:

```bash
python3 -m yarpgen --seed 1 --lang c --profile smoke --out /tmp/yg
```

`--lang` is `c` or `cxx`. `--profile` is `tiny`, `smoke`, or `campaign`
(larger expressions and arrays). `--count N` writes `seed_<n>/` under
`--out`. `--loops stencil,fusion` forces those idioms.

Files:

- `test.h` / `test.hpp` — types and declarations
- `func.c` / `func.cpp` — the generated function
- `driver.c` / `driver.cpp` — initializers, call, checksum
- `meta.json` — seed, skeleton, expected checksum

Compile the two translation units **without** `-flto` and run the binary.
It prints one unsigned integer.

```bash
gcc -std=c11 -fsigned-char -O0 -c -o func.o /tmp/yg/func.c
gcc -std=c11 -fsigned-char -O0 -c -o driver.o /tmp/yg/driver.c
gcc -o /tmp/yg/prog func.o driver.o
/tmp/yg/prog
```

C++ uses `g++ -std=c++17` (or `clang++`). One-dimensional C++ arrays may be
lowered to `std::array`, `std::vector`, `std::valarray`, or a C array.
Multi-dimensional arrays stay C arrays.

## Smoke test

Requires `gcc`, `g++`, `clang`, and `clang++` on `PATH`.

```bash
bash scripts/smoke.sh
# or
make smoke
```

The tests:

1. check the rewrite rules on corner values,
2. generate C and C++ programs, run them under ASan and UBSan, and compare
   `-O0`/`-O3` checksums with the generator's prediction,
3. reduce a synthetic failure and require the result to stay sanitizer-clean,
4. run a one-program campaign and require that a success leaves the results
   directory empty.

GitHub Actions runs the same script (`.github/workflows/smoke.yml`).

## Campaign on this machine

```bash
# a few minutes, distro compilers, failures only
YARPGEN_DURATION=10m bash scripts/campaign-local.sh --profile campaign --lang both

# or a fixed number of tests
python3 -m yarpgen.campaign \
  --config campaign/example-config.json \
  --tests 20 \
  --duration 30m \
  --out results \
  --sanitize-all
```

`--sanitize-all` runs ASan/UBSan on every program, including ones whose
checksums already agree. That is the right setting while changing the
generator. Leave it off for throughput; failures are still sanitized before
they are stored, and reduction always re-checks sanitizers.

`--jobs N` evaluates N programs at a time. Each program is still compiled
sequentially across the compiler matrix.

A successful program is deleted. A failure is copied to `results/cases/` and
appended to `results/index.jsonl`. ICE fingerprints are stored once (further
hits increment a counter). Each miscompile is stored under its own seed.
The case directory holds the sources, `meta.json`, `failure.json`, and
compiler stderr. After that, the line reducer tries to shrink `func.c`
without losing the failure or the sanitizer-clean property.

Optional external reducer, applied by the interestingness script if you point
a tool at a stored case yourself:

```bash
export YARPGEN_REDUCER=cvise          # or creduce
export YARPGEN_LANG=c
export YARPGEN_KIND=miscompile        # or ice
export YARPGEN_REF_CC=gcc
export YARPGEN_REF_FLAGS=-O0
export YARPGEN_BAD_CC=gcc
export YARPGEN_BAD_FLAGS=-O3
# from the case directory, after copying scripts/interestingness.sh
cvise interestingness.sh func.c
```

The in-process reducer does not need creduce or cvise installed.

MemorySanitizer is not on by default. It needs a fully instrumented C
library; ASan plus UBSan are the oracle this tree runs out of the box.
`-fsanitize=memory` can be added to a compiler entry in the JSON config
when that runtime exists.

## Docker: fresh GCC and LLVM

`docker/Dockerfile.toolchains` downloads and installs a release of GCC and
LLVM (defaults: GCC 14.2.0, LLVM 18.1.8, X86, assertions on in LLVM).
Expect tens of gigabytes of disk and a long build.

```bash
# both compilers
bash scripts/build-toolchains.sh

# GCC only, 8 jobs
BUILD_LLVM=0 JOBS=8 bash scripts/build-toolchains.sh
```

The image puts compilers on `PATH`, registers `/opt/gcc/lib64` and
`/opt/llvm/lib` with the dynamic linker, and writes
`/opt/yarpgen-compilers.json`. It also contains this tree under
`/opt/yarpgen` (the same layout as `docker/Dockerfile.campaign`), so the
default command can import `yarpgen.campaign`. Mount the repo when you want
the host tree instead of the copy baked into the image. Point the campaign
at the compiler fragment either way:

```bash
docker run --rm -v "$PWD":/src -w /src yarpgen-toolchains \
  python3 -m yarpgen.campaign \
    --config /opt/yarpgen-compilers.json \
    --duration 12h \
    --out /src/results \
    --profile campaign \
    --lang both
```

Merge `duration_sec`, `profile`, and `lang` into a copy of the generated
JSON if you would rather pass a single `--config`.

`docker/Dockerfile.campaign` is the smaller image: distro `gcc` and `clang`,
no from-source toolchain. Use it when you want the worker packaged and the
compilers you already trust.

```bash
docker build -f docker/Dockerfile.campaign -t yarpgen-campaign .
docker run --rm -v "$PWD/results":/results yarpgen-campaign
```

## Campaign on another machine over SSH

Nothing in the repo is a hostname or a key. Export them in the shell (see
`campaign/campaign.env.example`):

```bash
export YARPGEN_SSH_TARGET=user@build.example.com
export YARPGEN_SSH_KEY=$HOME/.ssh/id_ed25519
export YARPGEN_REMOTE_DIR=/var/tmp/yarpgen
export YARPGEN_DURATION=48h
export YARPGEN_CONFIG=campaign/example-config.json
bash scripts/campaign-ssh.sh
```

The script rsyncs the tree, then starts `python3 -m yarpgen.campaign` inside
`tmux` when `tmux` exists, otherwise under `nohup`. Pull failures back with:

```bash
bash scripts/fetch-results.sh
```

The remote needs Python 3, a C/C++ compiler pair, and enough disk for
object files. Install the toolchain image there first if you want the
from-source compilers. To use them, generate a config whose `cc`/`cxx`
fields are `/opt/gcc/bin/gcc` and `/opt/llvm/bin/clang` (the JSON written
into the toolchain image is a starting point) and pass it as
`YARPGEN_CONFIG`.

Give the remote campaign a long `--duration`. The worker stops at the
duration or at `--tests`, whichever comes first. It prints a progress line
every few tests and a `FAIL` line when something is stored. It does not
print or keep successful programs.

## Known limitations

- C and C++ only. ISPC and SYCL lowering from the PLDI paper is not in this
  tree.
- No floating point, function calls, pointer arithmetic, classes, templates,
  lambdas, enums, or dynamic allocation. Pointers are only `T*` to an input
  scalar. Bit-fields use `int` / `unsigned int` with width at most 7, which
  is the C rule subset; C++ allows a wider set.
- Loop trip counts are static and small. The even/odd split is the only
  partition. Induction variables step forward only.
- The reducer deletes lines. It is not C-Reduce. `scripts/interestingness.sh`
  is there for cvise or creduce when you want a harder shrink on a stored case.
- Miscompile classification is "which compilers disagreed", not opt-bisect.
  ICE dedup uses a normalized diagnostic line.
- Choice-sequence mutation is a single-draw API, not a coverage-guided loop.
- LP64 only. A 32-bit `long` would disagree with the value tracker.

## Next steps

- Lower the same loop IR to ISPC and SYCL, including masked / data-parallel
  loops.
- More partitions than even/odd, and opaque induction in a wider integer type.
- Search the choice sequence with compiler coverage (thesis chapter 4).
- Opt-bisect (or the GCC equivalent) as a miscompile fingerprint.
- Turn on MemorySanitizer in the oracle when an instrumented libc is part of
  the toolchain image.

"""Compiler command lines shared by the smoke test and the campaign runner."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


@lru_cache(maxsize=4)
def sanitizer_compiler(lang: str) -> str:
    """A compiler that can actually link ASan/UBSan.

    Some images ship ``clang`` without ``compiler-rt``, so preferring clang
    blindly turns every clean program into a false sanitizer failure.
    """
    candidates = ("g++", "clang++") if lang == "cxx" else ("gcc", "clang")
    for compiler in candidates:
        if not have(compiler):
            continue
        fd, binary = tempfile.mkstemp(prefix="yarpgen-san-")
        os.close(fd)
        cmd = [
            compiler,
            *standard_flags(compiler, lang),
            "-O0",
            "-fsanitize=undefined,address",
            "-fno-sanitize-recover=undefined,address",
            "-o",
            binary,
            "-x",
            "c++" if lang == "cxx" else "c",
            "-",
        ]
        result = subprocess.run(
            cmd,
            input="int main(void){return 0;}\n",
            text=True,
            capture_output=True,
            check=False,
        )
        Path(binary).unlink(missing_ok=True)
        if result.returncode == 0:
            return compiler
    raise SystemExit(f"no {lang} compiler can link -fsanitize=address,undefined")


@lru_cache(maxsize=8)
def libstdcxx_link_flags(compiler: str) -> tuple[str, ...]:
    """Library path when clang's selected GCC install has no libstdc++.so."""
    if "clang" not in Path(compiler).name:
        return ()
    fd, probe_name = tempfile.mkstemp(prefix="yarpgen-cxx-")
    os.close(fd)
    result = subprocess.run(
        [compiler, "-x", "c++", "-o", probe_name, "-"],
        input="int main(){return 0;}\n",
        text=True,
        capture_output=True,
        check=False,
    )
    Path(probe_name).unlink(missing_ok=True)
    if result.returncode == 0:
        return ()
    root = Path("/usr/lib/gcc/x86_64-linux-gnu")
    if not root.is_dir():
        return ()
    candidates = sorted(root.glob("*/libstdc++.so"), reverse=True)
    if not candidates:
        return ()
    return (f"-L{candidates[0].parent}",)


@lru_cache(maxsize=8)
def libstdcxx_flags(compiler: str) -> tuple[str, ...]:
    """Extra -isystem flags when clang cannot see the installed libstdc++.

    Some images ship clang beside a GCC whose version it does not auto-detect,
    so ``#include <vector>`` fails until the headers are named explicitly.
    """
    if "clang" not in Path(compiler).name:
        return ()
    probe = subprocess.run(
        [compiler, "-fsyntax-only", "-x", "c++", "-"],
        input="#include <vector>\n",
        text=True,
        capture_output=True,
        check=False,
    )
    if probe.returncode == 0:
        return ()
    root = Path("/usr/include/c++")
    if not root.is_dir():
        return ()
    versions = sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)
    for version in versions:
        if not (version / "vector").exists():
            continue
        flags = ["-isystem", str(version)]
        triple = Path("/usr/include") / "x86_64-linux-gnu" / "c++" / version.name
        if triple.is_dir():
            flags.extend(["-isystem", str(triple)])
        backward = version / "backward"
        if backward.is_dir():
            flags.extend(["-isystem", str(backward)])
        return tuple(flags)
    return ()


def standard_flags(compiler: str, lang: str) -> list[str]:
    std = "c11" if lang == "c" else "c++17"
    flags = ["-fsigned-char", "-Wno-unknown-pragmas", f"-std={std}"]
    if lang == "cxx":
        flags.extend(libstdcxx_flags(compiler))
        flags.extend(libstdcxx_link_flags(compiler))
    return flags


def source_files(directory: Path, lang: str) -> list[str]:
    suffix = ".cpp" if lang == "cxx" else ".c"
    files = sorted(p for p in directory.iterdir() if p.suffix == suffix)
    return [str(p) for p in files]

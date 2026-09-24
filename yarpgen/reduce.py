"""Test-case reduction that keeps the UB-free property.

C-Reduce-style delta debugging deletes lines only when the result still
triggers the original failure and still runs cleanly under ASan and UBSan.
That is the oracle from OOPSLA 2020: reduction may introduce undefined
behavior, so every candidate is rejected unless the sanitizers accept it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from yarpgen.compileutil import source_files, standard_flags

BEGIN = "/* YARPGEN_BODY_BEGIN */"
END = "/* YARPGEN_BODY_END */"


def split_body(text: str) -> tuple[str, list[str], str]:
    if BEGIN not in text or END not in text:
        raise ValueError("function file is missing reduction markers")
    pre, rest = text.split(BEGIN, 1)
    body, post = rest.split(END, 1)
    lines = body.splitlines(keepends=True)
    return pre, lines, post


def join_body(pre: str, lines: list[str], post: str) -> str:
    body = "".join(lines)
    if body and not body.endswith("\n"):
        body += "\n"
    return f"{pre}{BEGIN}\n{body}{END}{post}"


def ddmin(chunks: list[str], interesting, deadline: float) -> list[str]:
    """Classic delta debugging. ``interesting`` receives a candidate line list."""
    if not chunks:
        return chunks
    n = 2
    current = list(chunks)
    while len(current) >= 2 and time.time() < deadline:
        size = max(1, len(current) // n)
        subsets = [current[i : i + size] for i in range(0, len(current), size)]
        reduced = False
        for index in range(len(subsets)):
            if time.time() >= deadline:
                return current
            candidate = [line for i, subset in enumerate(subsets) if i != index for line in subset]
            if candidate and interesting(candidate):
                current = candidate
                n = max(2, n - 1)
                reduced = True
                break
        if reduced:
            continue
        for subset in subsets:
            if time.time() >= deadline:
                return current
            if len(subset) < len(current) and interesting(subset):
                current = list(subset)
                n = 2
                reduced = True
                break
        if reduced:
            continue
        if n >= len(current):
            break
        n = min(len(current), n * 2)
    return current


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def sanitizer_clean(case_dir: Path, lang: str, compiler: str, timeout_compile: float = 60, timeout_run: float = 5) -> bool:
    """True when -O0 ASan/UBSan compiles and the program exits 0 without reports."""
    binary = case_dir / ".san-bin"
    cmd = [
        compiler,
        *standard_flags(compiler, lang),
        "-O0",
        "-g",
        "-Werror=uninitialized",
        "-fsanitize=undefined,address",
        "-fno-sanitize-recover=undefined,address",
        "-o",
        str(binary),
        *source_files(case_dir, lang),
    ]
    try:
        compiled = _run(cmd, timeout_compile)
    except subprocess.TimeoutExpired:
        return False
    if compiled.returncode != 0:
        return False
    env = dict(os.environ)
    env["ASAN_OPTIONS"] = "detect_leaks=0:halt_on_error=1"
    env["UBSAN_OPTIONS"] = "halt_on_error=1:print_stacktrace=1"
    try:
        ran = subprocess.run(
            [str(binary)],
            capture_output=True,
            text=True,
            timeout=timeout_run,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False
    finally:
        binary.unlink(missing_ok=True)
    if ran.returncode != 0:
        return False
    noise = (ran.stderr or "") + (ran.stdout or "")
    lowered = noise.lower()
    if "runtime error:" in lowered or "addresssanitizer" in lowered or "undefinedbehaviorsanitizer" in lowered:
        return False
    return True


def reduce_file(func_path: Path, interesting, budget_sec: float = 60) -> bool:
    """Line-reduce ``func_path``. ``interesting`` must read the file from disk.

    The original file is restored if it was not interesting to begin with.
    Returns True when the final file is interesting.
    """
    original = func_path.read_text()
    pre, lines, post = split_body(original)
    deadline = time.time() + budget_sec

    def accept(new_lines: list[str]) -> bool:
        if time.time() >= deadline:
            return False
        func_path.write_text(join_body(pre, new_lines, post))
        try:
            return bool(interesting())
        except Exception:
            return False

    if not accept(lines):
        func_path.write_text(original)
        return False
    reduced = ddmin(lines, accept, deadline)
    func_path.write_text(join_body(pre, reduced, post))
    return True


def maybe_external_reduce(case_dir: Path, interesting_script: Path, budget_sec: float) -> str | None:
    """Run creduce or cvise when the user asked for them and the tool exists."""
    choice = os.environ.get("YARPGEN_REDUCER", "").strip().lower()
    if choice not in {"creduce", "cvise"}:
        return None
    tool = shutil.which(choice)
    if tool is None or not interesting_script.exists():
        return None
    func = next(case_dir.glob("func.*"), None)
    if func is None:
        return None
    cmd = [tool, "--timeout", str(max(1, int(budget_sec))), str(interesting_script), func.name]
    if choice == "creduce":
        cmd = [tool, str(interesting_script), func.name]
    try:
        subprocess.run(cmd, cwd=case_dir, timeout=budget_sec + 30, check=False)
    except subprocess.TimeoutExpired:
        return choice
    return choice

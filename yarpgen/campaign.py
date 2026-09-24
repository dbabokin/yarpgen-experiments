"""Differential campaign: generate, compile, keep only failures, reduce.

Successful runs are deleted. Storage keeps miscompilations, compiler crashes,
sanitizer failures, and other interesting outcomes. SSH and Docker wrappers
call this module; they do not embed host names or keys.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from yarpgen.compileutil import have, sanitizer_compiler, source_files, standard_flags
from yarpgen.generate import generate
from yarpgen.lower import write_program
from yarpgen.reduce import reduce_file, sanitizer_clean

ICE_MARKERS = (
    "internal compiler error",
    "please submit a bug report",
    "please attach the following",
    "stack dump:",
)


@dataclass
class Compiler:
    name: str
    cc: str
    cxx: str
    flags: list[str]


@dataclass
class RunResult:
    name: str
    compile_ok: bool
    ice: bool
    checksum: str | None
    returncode: int
    stderr: str
    timed_out: bool = False


@dataclass
class Outcome:
    kind: str
    seed: int
    lang: str
    fingerprint: str
    runs: list[RunResult] = field(default_factory=list)
    note: str = ""


def parse_duration(text: str) -> int:
    text = text.strip().lower()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smh]?)", text)
    if not match:
        raise ValueError(f"bad duration {text!r}")
    value = float(match.group(1))
    unit = match.group(2) or "s"
    return int(value * {"s": 1, "m": 60, "h": 3600}[unit])


def default_compilers() -> list[Compiler]:
    found = []
    if have("gcc") and have("g++"):
        found.append(Compiler("gcc-O0", "gcc", "g++", ["-O0"]))
        found.append(Compiler("gcc-O3", "gcc", "g++", ["-O3"]))
    if have("clang") and have("clang++"):
        found.append(Compiler("clang-O0", "clang", "clang++", ["-O0"]))
        found.append(Compiler("clang-O3", "clang", "clang++", ["-O3"]))
    if not found:
        raise SystemExit("no gcc/clang compilers found on PATH")
    return found


def load_config(path: Path | None, overrides: dict) -> dict:
    data: dict = {}
    if path is not None:
        data = json.loads(path.read_text())
    data.update({k: v for k, v in overrides.items() if v is not None})
    data.setdefault("lang", "c")
    data.setdefault("profile", "smoke")
    data.setdefault("jobs", 1)
    data.setdefault("reduce", True)
    data.setdefault("reduce_budget_sec", 90)
    data.setdefault("sanitize_all", False)
    data.setdefault("timeout_compile_sec", 60)
    data.setdefault("timeout_run_sec", 3)
    data.setdefault("store_dir", "results")
    data.setdefault("max_stored_per_fingerprint", 1)
    if "compilers" not in data:
        data["compilers"] = [
            {"name": c.name, "cc": c.cc, "cxx": c.cxx, "flags": c.flags} for c in default_compilers()
        ]
    return data


def compilers_from(data: dict) -> list[Compiler]:
    return [Compiler(c["name"], c["cc"], c.get("cxx", c["cc"]), list(c.get("flags", []))) for c in data["compilers"]]


def is_ice(returncode: int, stderr: str) -> bool:
    if returncode < 0:
        return True
    low = stderr.lower()
    return any(marker in low for marker in ICE_MARKERS)


def ice_fingerprint(compiler: str, stderr: str) -> str:
    interesting = []
    for line in stderr.splitlines():
        low = line.lower()
        if any(token in low for token in ("internal compiler error", "assertion", "error:", "please submit")):
            cleaned = re.sub(r"/[^\s:]+", "<path>", line.strip())
            cleaned = re.sub(r"0x[0-9a-fA-F]+", "0x", cleaned)
            interesting.append(cleaned[:240])
            if len(interesting) >= 3:
                break
    body = interesting[0] if interesting else "signal"
    return f"ice|{compiler}|{body}"


def miscompile_fingerprint(runs: list[RunResult], seed: int) -> str:
    groups: dict[str, list[str]] = {}
    for run in runs:
        if run.checksum is None:
            continue
        groups.setdefault(run.checksum, []).append(run.name)
    parts = ["+".join(sorted(names)) for names in groups.values()]
    # Seed is part of the id so distinct programs are not collapsed into one
    # disagreement pattern. ICE fingerprints stay message-based and are deduped.
    return f"miscompile|{seed}|" + "||".join(sorted(parts))


def _compile_and_run(case_dir: Path, lang: str, compiler: Compiler, timeout_compile: float, timeout_run: float) -> RunResult:
    binary = case_dir / f".bin-{compiler.name}"
    tool = compiler.cc if lang == "c" else compiler.cxx
    cmd = [tool, *standard_flags(tool, lang), *compiler.flags, "-o", str(binary), *source_files(case_dir, lang)]
    try:
        compiled = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_compile, check=False)
    except subprocess.TimeoutExpired:
        return RunResult(compiler.name, False, False, None, 124, "compile timeout", True)
    if compiled.returncode != 0:
        err = (compiled.stderr or "") + (compiled.stdout or "")
        return RunResult(compiler.name, False, is_ice(compiled.returncode, err), None, compiled.returncode, err[-8000:])
    try:
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=timeout_run, check=False)
    except subprocess.TimeoutExpired:
        binary.unlink(missing_ok=True)
        return RunResult(compiler.name, True, False, None, 124, "run timeout", True)
    binary.unlink(missing_ok=True)
    checksum = ran.stdout.strip().splitlines()[-1] if ran.stdout.strip() else ""
    return RunResult(compiler.name, True, False, checksum, ran.returncode, (ran.stderr or "")[-2000:], False)


def evaluate_seed(seed: int, lang: str, profile: str, case_dir: Path, comps: list[Compiler], cfg: dict) -> Outcome:
    program = generate(seed, lang, profile)
    write_program(program, case_dir)
    meta = {
        "seed": seed,
        "lang": lang,
        "profile": profile,
        "expected_checksum": program.expected_checksum,
        "skeleton": program.skeleton,
        "policy": program.policy_summary,
    }
    (case_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    runs = [
        _compile_and_run(
            case_dir,
            lang,
            comp,
            cfg["timeout_compile_sec"],
            cfg["timeout_run_sec"],
        )
        for comp in comps
    ]
    ices = [run for run in runs if run.ice]
    if ices:
        return Outcome("ice", seed, lang, ice_fingerprint(ices[0].name, ices[0].stderr), runs)
    compile_errors = [run for run in runs if not run.compile_ok and not run.timed_out]
    if compile_errors:
        err = compile_errors[0].stderr[-500:].replace("\n", " ")
        return Outcome("compile-error", seed, lang, f"compile|{compile_errors[0].name}|{err[:180]}", runs)
    timeouts = [run for run in runs if run.timed_out]
    checksums = {run.checksum for run in runs if run.checksum is not None and run.returncode == 0}
    if len(checksums) > 1:
        san = sanitizer_clean(case_dir, lang, sanitizer_compiler(lang))
        kind = "miscompile" if san else "sanitizer"
        return Outcome(kind, seed, lang, miscompile_fingerprint(runs, seed) if san else f"sanitizer|{seed}", runs)
    if timeouts:
        return Outcome("timeout", seed, lang, "timeout|" + ",".join(run.name for run in timeouts), runs)
    crashes = [run for run in runs if run.returncode not in (0, None) and run.compile_ok]
    if crashes:
        return Outcome("runtime", seed, lang, f"runtime|{crashes[0].name}|{crashes[0].returncode}", runs)
    if cfg.get("sanitize_all"):
        if not sanitizer_clean(case_dir, lang, sanitizer_compiler(lang)):
            return Outcome("sanitizer", seed, lang, f"sanitizer|{seed}", runs, "agreed checksum but sanitizer failed")
    return Outcome("ok", seed, lang, "ok", runs)


def _append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        try:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
        except Exception:
            pass
        handle.write(json.dumps(obj) + "\n")


def _fingerprint_count(store: Path, fingerprint: str) -> int:
    path = store / "fingerprints.json"
    if not path.exists():
        return 0
    data = json.loads(path.read_text() or "{}")
    return int(data.get(fingerprint, 0))


def _bump_fingerprint(store: Path, fingerprint: str) -> int:
    path = store / "fingerprints.json"
    data = json.loads(path.read_text() or "{}") if path.exists() else {}
    data[fingerprint] = int(data.get(fingerprint, 0)) + 1
    path.write_text(json.dumps(data, indent=2) + "\n")
    return data[fingerprint]


def store_failure(store: Path, case_dir: Path, outcome: Outcome, cfg: dict) -> Path | None:
    """Copy a failing case into ``store``. Returns the destination, or None if deduped."""
    if outcome.kind == "ok":
        return None
    store.mkdir(parents=True, exist_ok=True)
    count = _fingerprint_count(store, outcome.fingerprint)
    limit = int(cfg.get("max_stored_per_fingerprint", 1))
    # Miscompiles are rarer and fingerprints are coarse, so keep each seed
    # unless the cap for that disagreement pattern is already hit.
    store_body = count < limit
    seen = _bump_fingerprint(store, outcome.fingerprint)
    record = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "seed": outcome.seed,
        "lang": outcome.lang,
        "kind": outcome.kind,
        "fingerprint": outcome.fingerprint,
        "seen": seen,
        "stored": store_body,
        "note": outcome.note,
        "runs": [
            {
                "name": run.name,
                "compile_ok": run.compile_ok,
                "ice": run.ice,
                "checksum": run.checksum,
                "returncode": run.returncode,
                "timed_out": run.timed_out,
            }
            for run in outcome.runs
        ],
    }
    dest = None
    if store_body:
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        dest = store / "cases" / f"{outcome.kind}-{stamp}-seed{outcome.seed}"
        if dest.exists():
            dest = store / "cases" / f"{outcome.kind}-{stamp}-seed{outcome.seed}-{seen}"
        shutil.copytree(case_dir, dest, ignore=shutil.ignore_patterns(".bin-*", ".san-bin"))
        (dest / "failure.json").write_text(json.dumps(record, indent=2) + "\n")
        for run in outcome.runs:
            if run.stderr:
                (dest / f"stderr-{run.name}.log").write_text(run.stderr)
        record["path"] = str(dest)
    _append_jsonl(store / "index.jsonl", record)
    return dest


def _ref_and_bad(outcome: Outcome) -> tuple[RunResult, RunResult] | None:
    good = [run for run in outcome.runs if run.compile_ok and run.checksum and run.returncode == 0]
    if outcome.kind == "miscompile" and len(good) >= 2:
        ref = good[0]
        for other in good[1:]:
            if other.checksum != ref.checksum:
                return ref, other
    return None


def reduce_outcome(case_dir: Path, outcome: Outcome, comps: list[Compiler], cfg: dict) -> None:
    if outcome.kind not in {"miscompile", "ice"}:
        return
    func = next(case_dir.glob("func.*"), None)
    if func is None:
        return
    by_name = {comp.name: comp for comp in comps}
    lang = outcome.lang
    budget = float(cfg.get("reduce_budget_sec", 90))

    def interesting() -> bool:
        if not sanitizer_clean(case_dir, lang, sanitizer_compiler(lang), cfg["timeout_compile_sec"], cfg["timeout_run_sec"]):
            return False
        if outcome.kind == "ice":
            ice_run = next(run for run in outcome.runs if run.ice)
            comp = by_name.get(ice_run.name)
            if comp is None:
                return False
            again = _compile_and_run(case_dir, lang, comp, cfg["timeout_compile_sec"], cfg["timeout_run_sec"])
            return again.ice
        pair = _ref_and_bad(outcome)
        if pair is None:
            return False
        ref_c = by_name[pair[0].name]
        bad_c = by_name[pair[1].name]
        ref = _compile_and_run(case_dir, lang, ref_c, cfg["timeout_compile_sec"], cfg["timeout_run_sec"])
        bad = _compile_and_run(case_dir, lang, bad_c, cfg["timeout_compile_sec"], cfg["timeout_run_sec"])
        return bool(ref.checksum and bad.checksum and ref.checksum != bad.checksum and ref.returncode == 0 and bad.returncode == 0)

    reduce_file(func, interesting, budget)


def _prepare_task(cfg: dict, store: Path, tested: int, seed_base: int) -> tuple:
    seed = (seed_base + tested * 9973) & 0x7FFFFFFF
    lang_mode = cfg.get("lang", "c")
    lang = lang_mode
    if lang_mode == "both":
        lang = "c" if tested % 2 else "cxx"
    work = Path(cfg.get("work_dir", store / ".work")) / f"s{seed}-{os.getpid()}"
    return seed, lang, work


def _run_one(seed: int, lang: str, work: Path, comps: list[Compiler], cfg: dict) -> Outcome:
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    try:
        return evaluate_seed(seed, lang, cfg.get("profile", "smoke"), work, comps, cfg)
    except Exception as exc:
        (work / "generator-error.txt").write_text(traceback.format_exc())
        return Outcome("generator-crash", seed, lang, f"generator|{type(exc).__name__}", [], str(exc))


def _finish(store: Path, work: Path, outcome: Outcome, comps: list[Compiler], cfg: dict, stats: dict) -> None:
    if outcome.kind == "ok":
        stats["ok"] += 1
        shutil.rmtree(work, ignore_errors=True)
        return
    stats["fail"] += 1
    dest = store_failure(store, work, outcome, cfg)
    shutil.rmtree(work, ignore_errors=True)
    if dest is not None and cfg.get("reduce", True):
        try:
            reduce_outcome(dest, outcome, comps, cfg)
        except Exception:
            (dest / "reduce-error.txt").write_text(traceback.format_exc())
    print(f"FAIL kind={outcome.kind} seed={outcome.seed} lang={outcome.lang} stored={dest}", flush=True)


def run_campaign(cfg: dict) -> dict:
    comps = compilers_from(cfg)
    store = Path(cfg["store_dir"])
    duration = cfg.get("duration_sec")
    max_tests = cfg.get("tests")
    if duration is None and max_tests is None:
        raise SystemExit("set duration_sec or tests")
    deadline = time.time() + float(duration if duration is not None else 10**9)
    stats = {"ok": 0, "fail": 0}
    seed_base = int(cfg.get("seed", int(time.time())))
    jobs = max(1, int(cfg.get("jobs") or 1))
    tested = 0
    if jobs == 1:
        while time.time() < deadline and (max_tests is None or tested < int(max_tests)):
            tested += 1
            seed, lang, work = _prepare_task(cfg, store, tested, seed_base)
            outcome = _run_one(seed, lang, work, comps, cfg)
            _finish(store, work, outcome, comps, cfg, stats)
            if tested % 10 == 0:
                print(f"progress tested={tested} ok={stats['ok']} failures={stats['fail']}", flush=True)
    else:
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(max_workers=jobs) as pool:
            while time.time() < deadline and (max_tests is None or tested < int(max_tests)):
                batch = []
                while len(batch) < jobs and time.time() < deadline and (max_tests is None or tested < int(max_tests)):
                    tested += 1
                    seed, lang, work = _prepare_task(cfg, store, tested, seed_base)
                    batch.append((seed, lang, work))
                futures = [
                    pool.submit(_run_one, seed, lang, work, comps, cfg) for seed, lang, work in batch
                ]
                for (seed, lang, work), future in zip(batch, futures):
                    outcome = future.result()
                    _finish(store, work, outcome, comps, cfg, stats)
                print(f"progress tested={tested} ok={stats['ok']} failures={stats['fail']}", flush=True)
    work_root = Path(cfg.get("work_dir", store / ".work"))
    shutil.rmtree(work_root, ignore_errors=True)
    print(f"done tested={tested} ok={stats['ok']} failures={stats['fail']} store={store}", flush=True)
    return {"tested": tested, **stats}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run a YARPGen differential campaign")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--duration", help="for example 30s, 12h")
    parser.add_argument("--tests", type=int)
    parser.add_argument("--out", type=Path, help="failure store directory")
    parser.add_argument("--lang", choices=["c", "cxx", "both"])
    parser.add_argument("--profile", choices=["tiny", "smoke", "campaign"])
    parser.add_argument("--jobs", type=int)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--sanitize-all", action="store_true")
    parser.add_argument("--no-reduce", action="store_true")
    args = parser.parse_args(argv)
    overrides = {
        "duration_sec": parse_duration(args.duration) if args.duration else None,
        "tests": args.tests,
        "store_dir": str(args.out) if args.out else None,
        "lang": args.lang,
        "profile": args.profile,
        "jobs": args.jobs,
        "seed": args.seed,
        "sanitize_all": True if args.sanitize_all else None,
        "reduce": False if args.no_reduce else None,
    }
    cfg = load_config(args.config, overrides)
    if args.out:
        cfg["store_dir"] = str(args.out)
    run_campaign(cfg)


if __name__ == "__main__":
    main()

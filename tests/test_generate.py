"""Generated programs match the tracked checksum and stay sanitizer-clean."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from yarpgen.compileutil import standard_flags
from yarpgen.generate import generate
from yarpgen.lower import render_files, write_program


def _compile_and_run(directory: Path, lang: str, compiler: str, extra: list[str]) -> tuple[int, str, str]:
    binary = directory / "prog"
    sources = sorted(str(p) for p in directory.iterdir() if p.suffix in {".c", ".cpp"})
    compiled = subprocess.run(
        [compiler, *standard_flags(compiler, lang), *extra, "-o", str(binary), *sources],
        capture_output=True,
        text=True,
        check=False,
    )
    if compiled.returncode != 0:
        return compiled.returncode, "", compiled.stderr
    env = dict(**{k: v for k, v in __import__("os").environ.items()})
    env["ASAN_OPTIONS"] = "detect_leaks=0:halt_on_error=1"
    env["UBSAN_OPTIONS"] = "halt_on_error=1"
    ran = subprocess.run([str(binary)], capture_output=True, text=True, check=False, env=env)
    return ran.returncode, ran.stdout.strip(), ran.stderr


class GenerateTest(unittest.TestCase):
    def test_same_seed_is_deterministic(self):
        first = generate(11, "c", "tiny")
        second = generate(11, "c", "tiny")
        self.assertEqual(first.expected_checksum, second.expected_checksum)
        self.assertEqual(first.choices, second.choices)
        self.assertEqual(render_files(first)["func.c"], render_files(second)["func.c"])

    def test_choice_flip_changes_the_sequence(self):
        first = generate(11, "c", "smoke")
        replacement = 3 if first.choices[0] != 3 else 5
        flipped = generate(11, "c", "smoke", flips={0: replacement})
        self.assertEqual(flipped.choices[0], replacement)
        self.assertNotEqual(flipped.choices, first.choices)

    def test_programs_match_sanitizers_and_optimizers(self):
        cases = [
            (3, "c", "smoke", None),
            (4, "cxx", "smoke", ["stencil", "reduction"]),
        ]
        for seed, lang, profile, loops in cases:
            with self.subTest(seed=seed, lang=lang):
                program = generate(seed, lang, profile, force_loops=loops)
                directory = Path(tempfile.mkdtemp(prefix="yarpgen-"))
                try:
                    write_program(program, directory)
                    gcc = "gcc" if lang == "c" else "g++"
                    clang = "clang" if lang == "c" else "clang++"
                    code, out, err = _compile_and_run(
                        directory,
                        lang,
                        gcc,
                        ["-O0", "-fsanitize=undefined,address", "-fno-sanitize-recover=undefined,address"],
                    )
                    self.assertEqual(code, 0, err[-1500:])
                    self.assertNotIn("runtime error", err.lower())
                    self.assertEqual(out, str(program.expected_checksum))
                    for compiler, opt in ((gcc, "-O3"), (clang, "-O0"), (clang, "-O3")):
                        code, again, err = _compile_and_run(directory, lang, compiler, [opt])
                        self.assertEqual(code, 0, f"{compiler} {opt}\n{err[-800:]}")
                        self.assertEqual(again, out, f"{compiler} {opt}")
                finally:
                    shutil.rmtree(directory)


if __name__ == "__main__":
    unittest.main()

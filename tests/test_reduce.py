"""Reduction keeps a synthetic failure and rejects steps that break it."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from yarpgen.compileutil import standard_flags
from yarpgen.reduce import reduce_file


SOURCE = """\
#include <stdio.h>
int out;
void test_func(void) {
/* YARPGEN_BODY_BEGIN */
    out = 7; /* KEEP */
    int j0 = 1;
    j0 = j0;
    int j1 = 2;
    j1 = j1;
    int j2 = 3;
    j2 = j2;
/* YARPGEN_BODY_END */
}
int main(void) {
    test_func();
    printf("%d\\n", out);
    return 0;
}
"""


class ReduceTest(unittest.TestCase):
    def test_synthetic_failure_shrinks_and_stays_sanitizer_clean(self):
        directory = Path(tempfile.mkdtemp(prefix="yarpgen-reduce-"))
        func = directory / "func.c"
        func.write_text(SOURCE)
        binary = directory / "prog"

        def interesting() -> bool:
            if "KEEP" not in func.read_text():
                return False
            compiled = subprocess.run(
                [
                    "gcc",
                    *standard_flags("gcc", "c"),
                    "-O0",
                    "-Werror=uninitialized",
                    "-fsanitize=undefined,address",
                    "-fno-sanitize-recover=undefined,address",
                    "-o",
                    str(binary),
                    str(func),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if compiled.returncode != 0:
                return False
            ran = subprocess.run([str(binary)], capture_output=True, text=True, check=False)
            if ran.returncode != 0:
                return False
            if "runtime error" in (ran.stderr or "").lower():
                return False
            return ran.stdout.strip() == "7"

        self.assertTrue(interesting())
        before = func.read_text().count("\n")
        self.assertTrue(reduce_file(func, interesting, budget_sec=60))
        after = func.read_text()
        self.assertIn("KEEP", after)
        self.assertLess(after.count("\n"), before)
        self.assertNotIn("j0", after)
        self.assertTrue(interesting())


if __name__ == "__main__":
    unittest.main()

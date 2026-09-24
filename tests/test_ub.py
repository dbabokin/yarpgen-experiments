"""Rewrite rules stay inside the representable range (OOPSLA 2020, Table 1)."""

import unittest

from yarpgen.ir import const
from yarpgen.typesys import SINT, UINT
from yarpgen.ub import safe_add, safe_div, safe_mod, safe_mul, safe_neg, safe_shl, safe_shr, safe_sub


CORNERS = (
    0,
    1,
    -1,
    2,
    -2,
    17,
    -17,
    SINT.maxv(),
    SINT.minv(),
    SINT.maxv() - 1,
    SINT.minv() + 1,
    64,
    -64,
)


class RewriteTest(unittest.TestCase):
    def test_signed_arithmetic_fits(self):
        for left in CORNERS:
            for right in CORNERS:
                for fn in (safe_add, safe_sub, safe_mul, safe_div, safe_mod):
                    expr = fn(const(SINT, left), const(SINT, right))
                    self.assertTrue(SINT.fits(expr.value), fn.__name__)

    def test_division_by_zero_becomes_multiply(self):
        expr = safe_div(const(SINT, 5), const(SINT, 0))
        self.assertEqual(expr.op, "*")
        self.assertEqual(expr.value, 0)
        expr = safe_mod(const(UINT, 9), const(UINT, 0))
        self.assertEqual(expr.op, "*")

    def test_min_divided_by_negative_one(self):
        expr = safe_div(const(SINT, SINT.minv()), const(SINT, -1))
        self.assertEqual(expr.op, "-")
        self.assertTrue(SINT.fits(expr.value))

    def test_negation_of_min(self):
        expr = safe_neg(const(SINT, SINT.minv()))
        self.assertEqual(expr.op, "+")
        self.assertEqual(expr.value, SINT.minv())

    def test_shifts_are_defined(self):
        amounts = (0, 1, -1, 31, 32, 40, SINT.minv(), -5, 7)
        values = (0, 1, -1, -5, 1024, SINT.maxv(), SINT.minv(), 255)
        for value in values:
            for amount in amounts:
                for fn in (safe_shl, safe_shr):
                    expr = fn(const(SINT, value), const(SINT, amount))
                    self.assertTrue(SINT.fits(expr.value))
                    if expr.kind == "binary" and expr.op in {"<<", ">>"}:
                        self.assertGreaterEqual(expr.kids[0].value, 0)
                        self.assertGreaterEqual(expr.kids[1].value, 0)
                        self.assertLess(expr.kids[1].value, 32)

    def test_unsigned_wraps(self):
        expr = safe_add(const(UINT, UINT.maxv()), const(UINT, 2))
        self.assertEqual(expr.op, "+")
        self.assertEqual(expr.value, 1)


if __name__ == "__main__":
    unittest.main()

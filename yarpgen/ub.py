"""Local rewrites that keep every emitted operation free of undefined behavior.

The rules follow Table 1 of Livinskii, Babokin, and Regehr (OOPSLA 2020): when a
concrete value would make an operation undefined, replace it with a nearby
operation that is defined for those values. Signed left-shift results that are
not representable, and negative left operands of shifts, are rewritten in the
same local style (the paper's MSB adjustment and the ``(a + MAX)`` rewrite).
"""

from __future__ import annotations

from yarpgen.ir import E, binary, cast, const, ternary, unary
from yarpgen.typesys import (
    SINT,
    SLL,
    IntType,
    c_div,
    c_mod,
    convert,
    promote,
    uac,
)


class UBError(RuntimeError):
    pass


def _as_promoted(expr: E) -> E:
    dest = promote(expr.typ)
    value = convert(expr.value, dest)
    if expr.typ == dest and expr.value == value:
        return expr
    return cast(expr, dest, value)


def _assert_fits(value: int, typ: IntType, what: str) -> None:
    if typ.signed and not typ.fits(value):
        raise UBError(f"{what} produced {value}, which does not fit in {typ.name}")


def safe_add(left: E, right: E) -> E:
    typ = uac(left.typ, right.typ)
    lv, rv = convert(left.value, typ), convert(right.value, typ)
    if not typ.signed:
        return binary("+", left, right, typ, convert(lv + rv, typ))
    if typ.fits(lv + rv):
        return binary("+", left, right, typ, lv + rv)
    # Same-sign overflow only; the difference then fits.
    diff = lv - rv
    _assert_fits(diff, typ, "add->sub")
    return binary("-", left, right, typ, diff)


def safe_sub(left: E, right: E) -> E:
    typ = uac(left.typ, right.typ)
    lv, rv = convert(left.value, typ), convert(right.value, typ)
    if not typ.signed:
        return binary("-", left, right, typ, convert(lv - rv, typ))
    if typ.fits(lv - rv):
        return binary("-", left, right, typ, lv - rv)
    total = lv + rv
    _assert_fits(total, typ, "sub->add")
    return binary("+", left, right, typ, total)


def safe_mul(left: E, right: E) -> E:
    typ = uac(left.typ, right.typ)
    lv, rv = convert(left.value, typ), convert(right.value, typ)
    if not typ.signed:
        return binary("*", left, right, typ, convert(lv * rv, typ))
    product = lv * rv
    if typ.fits(product):
        return binary("*", left, right, typ, product)
    if rv == 0 or lv == 0:
        return binary("*", left, right, typ, 0)
    # MIN * -1 is the one division that would itself be undefined.
    if not (lv == typ.minv() and rv == -1):
        return binary("/", left, right, typ, c_div(lv, rv))
    diff = lv - rv
    _assert_fits(diff, typ, "mul->sub")
    return binary("-", left, right, typ, diff)


def safe_div(left: E, right: E) -> E:
    return _safe_divmod(left, right, "/")


def safe_mod(left: E, right: E) -> E:
    return _safe_divmod(left, right, "%")


def _safe_divmod(left: E, right: E, op: str) -> E:
    typ = uac(left.typ, right.typ)
    lv, rv = convert(left.value, typ), convert(right.value, typ)
    unsafe = rv == 0 or (typ.signed and lv == typ.minv() and rv == -1)
    if unsafe:
        if rv == 0:
            return binary("*", left, right, typ, 0)
        diff = lv - rv
        _assert_fits(diff, typ, "div->sub")
        return binary("-", left, right, typ, diff)
    value = c_div(lv, rv) if op == "/" else c_mod(lv, rv)
    _assert_fits(value, typ, op)
    return binary(op, left, right, typ, value)


def safe_neg(expr: E) -> E:
    promoted = _as_promoted(expr)
    typ = promoted.typ
    value = promoted.value
    if not typ.signed:
        return unary("-", promoted, typ, convert(-value, typ))
    if value == typ.minv():
        # Table 1: -MIN -> +MIN.
        return unary("+", promoted, typ, value)
    return unary("-", promoted, typ, -value)


def safe_not(expr: E) -> E:
    """Logical not. Defined for every integer."""
    promoted = _as_promoted(expr)
    return unary("!", promoted, SINT, 0 if promoted.value != 0 else 1)


def safe_bitnot(expr: E) -> E:
    promoted = _as_promoted(expr)
    typ = promoted.typ
    raw = ~promoted.value
    return unary("~", promoted, typ, convert(raw, typ))


def safe_plus(expr: E) -> E:
    promoted = _as_promoted(expr)
    return unary("+", promoted, promoted.typ, promoted.value)


def _force_nonnegative(expr: E) -> E:
    """Rewrite a promoted operand so a later shift sees a non-negative value.

    Mirrors the paper's ``(a + MAX)`` adjustment. ``MIN + MAX`` is -1, so a
    further ``+ 1`` (defined) yields 0 and the shift is still present.
    """
    promoted = _as_promoted(expr)
    typ = promoted.typ
    value = promoted.value
    if not typ.signed or value >= 0:
        return promoted
    added = safe_add(promoted, const(typ, typ.maxv()))
    if added.value < 0:
        added = safe_add(added, const(typ, 1))
    if added.value < 0:
        raise UBError("failed to force a non-negative shift operand")
    return added


def _shl_limit(value: int, typ: IntType) -> int:
    """Exclusive upper bound on a defined left-shift amount."""
    bits = typ.bits
    if typ.signed and value < 0:
        return 0
    if (not typ.signed) or value == 0:
        return bits
    current = value
    amount = 0
    while amount + 1 < bits and current <= (typ.maxv() >> 1):
        current *= 2
        amount += 1
    return amount + 1


def _try_add_const(rhs: E, delta: int, target: int) -> E | None:
    if delta == 0:
        return rhs
    if SINT.fits(delta):
        const_type: IntType = SINT
    elif SLL.fits(delta):
        const_type = SLL
    else:
        return None
    rhs_type = promote(rhs.typ)
    rhs_value = convert(rhs.value, rhs_type)
    result_type = uac(rhs_type, const_type)
    left_v = convert(rhs_value, result_type)
    right_v = convert(delta, result_type)
    if result_type.signed:
        if not result_type.fits(left_v + right_v):
            return None
        result = left_v + right_v
    else:
        result = convert(left_v + right_v, result_type)
    if result != target:
        return None
    return binary("+", rhs, const(const_type, delta), result_type, result)


def _clamp_shift_amount(rhs: E, limit: int) -> E | None:
    if limit <= 0:
        return None
    rhs_type = promote(rhs.typ)
    rhs_value = convert(rhs.value, rhs_type)
    if 0 <= rhs_value < limit:
        return rhs
    target = rhs_value % limit
    adjusted = _try_add_const(rhs, target - rhs_value, target)
    if adjusted is not None:
        return adjusted
    return const(SINT, target)


def safe_shl(left: E, right: E) -> E:
    forced = _force_nonnegative(left)
    typ = forced.typ
    limit = _shl_limit(forced.value, typ)
    amount = _clamp_shift_amount(right, limit)
    if amount is None:
        return forced
    shifted = forced.value * (1 << amount.value)
    _assert_fits(shifted, typ, "shl")
    if typ.signed and not typ.fits(shifted):
        raise UBError("shl still out of range")
    if not typ.signed:
        shifted = convert(shifted, typ)
    return binary("<<", forced, amount, typ, shifted)


def safe_shr(left: E, right: E) -> E:
    forced = _force_nonnegative(left)
    typ = forced.typ
    amount = _clamp_shift_amount(right, typ.bits)
    if amount is None:
        return forced
    value = forced.value >> amount.value
    _assert_fits(value, typ, "shr")
    return binary(">>", forced, amount, typ, value)


def safe_bitwise(op: str, left: E, right: E) -> E:
    typ = uac(left.typ, right.typ)
    lv, rv = convert(left.value, typ), convert(right.value, typ)
    if op == "&":
        result = lv & rv
    elif op == "|":
        result = lv | rv
    elif op == "^":
        result = lv ^ rv
    else:
        raise UBError(op)
    return binary(op, left, right, typ, convert(result, typ))


def safe_compare(op: str, left: E, right: E) -> E:
    typ = uac(left.typ, right.typ)
    lv, rv = convert(left.value, typ), convert(right.value, typ)
    table = {
        "<": lv < rv,
        ">": lv > rv,
        "<=": lv <= rv,
        ">=": lv >= rv,
        "==": lv == rv,
        "!=": lv != rv,
    }
    return binary(op, left, right, SINT, 1 if table[op] else 0)


def safe_logical(op: str, left: E, right: E) -> E:
    if op == "&&":
        value = 1 if (left.value != 0 and right.value != 0) else 0
    elif op == "||":
        value = 1 if (left.value != 0 or right.value != 0) else 0
    else:
        raise UBError(op)
    return binary(op, left, right, SINT, value)


def safe_ternary(cond: E, then: E, els: E, dest: IntType) -> E:
    then_f = fit_to_type(then, dest)
    els_f = fit_to_type(els, dest)
    value = then_f.value if cond.value != 0 else els_f.value
    return ternary(cond, then_f, els_f, dest, value)


def fit_to_type(expr: E, dest: IntType) -> E:
    """Conversion that is defined and stable across GCC and Clang on LP64.

    Out-of-range signed destinations are masked through an unsigned type so the
    stored value does not depend on implementation-defined truncation.
    """
    if dest.name == "bool":
        value = 1 if expr.value != 0 else 0
        if expr.typ == dest and expr.value == value:
            return expr
        return cast(expr, dest, value)
    if not dest.signed:
        value = convert(expr.value, dest)
        if expr.typ == dest and expr.value == value:
            return expr
        return cast(expr, dest, value)
    if dest.fits(expr.value) and expr.typ == dest:
        return expr
    if dest.fits(expr.value):
        return cast(expr, dest, expr.value)
    from yarpgen.typesys import UCHAR, UINT, ULL, ULONG, USHORT

    unsigned = {8: UCHAR, 16: USHORT, 32: UINT, 64: ULONG if dest.rank == 4 else ULL}[dest.bits]
    as_unsigned = convert(expr.value, unsigned)
    masked = as_unsigned & dest.maxv()
    narrowed = binary(
        "&",
        cast(expr, unsigned, as_unsigned),
        const(unsigned, dest.maxv()),
        unsigned,
        masked,
    )
    return cast(narrowed, dest, masked)


def apply_binary(op: str, left: E, right: E) -> E:
    if op == "+":
        return safe_add(left, right)
    if op == "-":
        return safe_sub(left, right)
    if op == "*":
        return safe_mul(left, right)
    if op == "/":
        return safe_div(left, right)
    if op == "%":
        return safe_mod(left, right)
    if op == "<<":
        return safe_shl(left, right)
    if op == ">>":
        return safe_shr(left, right)
    if op in {"&", "|", "^"}:
        return safe_bitwise(op, left, right)
    if op in {"<", ">", "<=", ">=", "==", "!="}:
        return safe_compare(op, left, right)
    if op in {"&&", "||"}:
        return safe_logical(op, left, right)
    raise UBError(f"unknown binary op {op}")


def apply_unary(op: str, expr: E) -> E:
    if op == "-":
        return safe_neg(expr)
    if op == "+":
        return safe_plus(expr)
    if op == "~":
        return safe_bitnot(expr)
    if op == "!":
        return safe_not(expr)
    raise UBError(f"unknown unary op {op}")

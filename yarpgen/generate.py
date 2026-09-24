"""Top-down generator: skeleton, then expressions, with concrete value tracking.

Scalar code follows OOPSLA 2020 (inputs / mixed / outputs, contexts, CSE buffer,
constant policies, shuffled distributions). Loops follow PLDI 2023: iteration
spaces are chosen first, input arrays are uniform or even/odd, and induction
variables are not used as arbitrary arithmetic leaves.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from yarpgen.ir import (
    Bound,
    E,
    StmtAssign,
    StmtBlock,
    StmtDecl,
    StmtIf,
    StmtLoop,
    const,
    deref,
    expr_names,
    member,
    name,
    stmt_names,
    subscript,
)
from yarpgen.policy import CONTEXTS, Policy, shuffle_policy
from yarpgen.rng import Rng
from yarpgen.typesys import (
    SINT,
    UCHAR,
    UINT,
    IntType,
    convert,
    mix_hash,
)
from yarpgen.ub import apply_binary, apply_unary, fit_to_type, safe_add


@dataclass
class Scalar:
    name: str
    typ: IntType
    value: int
    role: str  # input, mixed, output, param
    const: bool = False
    init: int | None = None

    def __post_init__(self) -> None:
        # Outputs are initialized to 0 in the driver so a dropped store changes
        # the checksum. Inputs and mixed variables keep the value they are
        # created with; later assignments update only ``value``.
        if self.init is None:
            self.init = 0 if self.role == "output" else self.value


@dataclass
class Field:
    name: str
    typ: IntType
    value: int
    bitwidth: int | None = None


@dataclass
class StructType:
    name: str
    fields: list[Field]


@dataclass
class StructVar:
    name: str
    stype: StructType
    role: str = "input"


@dataclass
class ArrayVar:
    name: str
    elem: IntType
    dims: tuple[int, ...]
    values: list
    role: str
    storage: str = "c_array"
    access: str = "subscript"
    init_values: list = field(default_factory=list)


@dataclass
class PointerVar:
    name: str
    target: str
    typ: IntType


@dataclass
class HashItem:
    expr: str
    value: int
    typ: IntType


@dataclass
class Program:
    lang: str
    seed: int
    profile: str
    structs: list[StructType]
    struct_vars: list[StructVar]
    scalars: list[Scalar]
    arrays: list[ArrayVar]
    pointers: list[PointerVar]
    params: list[Scalar]
    body: list
    hash_items: list[HashItem]
    expected_checksum: int
    skeleton: str
    policy_summary: dict
    choices: list[int] = field(default_factory=list)


class Env:
    def __init__(self) -> None:
        self.scalars: dict[str, Scalar] = {}
        self.locals: dict[str, tuple[IntType, int]] = {}
        self.arrays: dict[str, ArrayVar] = {}
        self.struct_vars: dict[str, StructVar] = {}
        self.pointers: dict[str, PointerVar] = {}
        self.structs: list[StructType] = []
        self.cse: list[E] = []
        self.const_buf: list[tuple[IntType, int]] = []
        self.counter = 0
        self.in_loop = False
        self.reads: list[E] = []
        self.out_arrays: list[str] = []

    def fresh(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    def add_scalar(self, slot: Scalar) -> Scalar:
        self.scalars[slot.name] = slot
        return slot

    def snapshot(self) -> dict:
        return {
            "scalars": {k: (s.value,) for k, s in self.scalars.items()},
            "locals": dict(self.locals),
            "arrays": {k: _copy_values(a.values) for k, a in self.arrays.items()},
        }

    def restore_values(self, snap: dict) -> None:
        for name, (value,) in snap["scalars"].items():
            self.scalars[name].value = value
        # Drop locals created after the snapshot; restore the rest.
        self.locals = dict(snap["locals"])
        for name, values in snap["arrays"].items():
            self.arrays[name].values = values


def _copy_values(values):
    if isinstance(values, list):
        return [_copy_values(v) for v in values]
    return values


def _fill(dims: tuple[int, ...], value: int):
    if len(dims) == 1:
        return [value for _ in range(dims[0])]
    return [_fill(dims[1:], value) for _ in range(dims[0])]


def _value_at(values, index: tuple[int, ...]) -> int:
    node = values
    for i in index:
        node = node[i]
    return node


def _set_at(values, index: tuple[int, ...], value: int) -> None:
    node = values
    for i in index[:-1]:
        node = node[i]
    node[index[-1]] = value


def _row_major(dims: tuple[int, ...]) -> list[tuple[int, ...]]:
    if not dims:
        return [()]
    rest = _row_major(dims[1:])
    out = []
    for i in range(dims[0]):
        for tail in rest:
            out.append((i,) + tail)
    return out


def _iter_space(start: int, end: int, step: int) -> list[int] | None:
    if step <= 0 or start >= end:
        return None
    values: list[int] = []
    i = start
    limit = 2**31 - 1
    while i < end:
        values.append(i)
        if i > limit - step:
            return None
        i += step
        if len(values) > 48:
            return None
    return values or None


def _pick_type(rng: Rng, policy: Policy, *, bool_ok: bool = True) -> IntType:
    weights = policy.type_weights
    if not bool_ok:
        weights = [(w, t) for w, t in weights if t.name != "bool"] or policy.type_weights
    return rng.weighted(weights)


def _clamp_value(typ: IntType, value: int) -> int:
    if typ.fits(value):
        return value
    if typ.name == "bool":
        return 1 if value else 0
    if not typ.signed:
        return convert(value, typ)
    span = typ.maxv() - typ.minv() + 1
    return typ.minv() + (value - typ.minv()) % span


def _gen_const_value(rng: Rng, policy: Policy, env: Env, typ: IntType) -> int:
    if typ.name == "bool":
        return rng.randint(0, 1)
    kind = rng.weighted(policy.const_weights)
    value: int
    if kind == "small":
        value = _clamp_value(typ, rng.randint(-16, 16))
    elif kind == "extreme":
        candidates = [typ.minv(), typ.maxv(), 0, 1, -1]
        if typ.maxv() > 2:
            candidates.extend([typ.minv() + 1, typ.maxv() - 1])
        value = rng.choice([c for c in candidates if typ.fits(c)])
    elif kind == "bitblock":
        patterns = [0x0F, 0xF0, 0xFF, 0x7F, 0x55, 0xAA, 0xFFFF, 0xFF00, 0x0FF0, 0x7FFFFFFF]
        raw = rng.choice(patterns)
        if rng.chance(0.5) and typ.signed:
            raw = -raw
        value = _clamp_value(typ, raw)
    elif kind == "reuse" and env.const_buf:
        base_t, base_v = rng.choice(env.const_buf)
        mode = rng.choice(["id", "neg", "not"])
        raw = base_v
        if mode == "neg":
            raw = -base_v
        elif mode == "not":
            raw = ~base_v
        value = _clamp_value(typ, raw)
    else:
        # Full-range draws on 64-bit types are legal; bias toward a 32-bit window
        # half the time so small and large constants both show up under shuffling.
        if typ.bits > 32 and rng.chance(0.5):
            value = _clamp_value(typ, rng.randint(-(2**31), 2**31 - 1))
        else:
            value = rng.randint(typ.minv(), typ.maxv())
    env.const_buf.append((typ, value))
    if len(env.const_buf) > 24:
        del env.const_buf[0]
    return value


def _const_expr(rng: Rng, policy: Policy, env: Env, typ: IntType | None = None) -> E:
    typ = typ or _pick_type(rng, policy)
    return const(typ, _gen_const_value(rng, policy, env, typ))


def _readable_names(env: Env) -> list[tuple[str, IntType, int, bool]]:
    found = []
    for slot in env.scalars.values():
        if slot.role in {"input", "param", "mixed"}:
            found.append((slot.name, slot.typ, slot.value, slot.role == "input" or slot.const or slot.role == "param"))
    for nm, (typ, value) in env.locals.items():
        found.append((nm, typ, value, False))
    return found


def _gen_leaf(rng: Rng, policy: Policy, env: Env) -> E:
    kind = rng.weighted(policy.leaf_weights)
    if kind == "cse" and env.cse and rng.chance(policy.p_cse_use):
        return rng.choice(env.cse)
    if kind == "array" and env.reads:
        return rng.choice(env.reads)
    if kind == "array" and not env.in_loop:
        arrays = [a for a in env.arrays.values() if a.role == "input" and len(a.dims) == 1]
        if arrays:
            arr = rng.choice(arrays)
            idx = rng.randint(0, arr.dims[0] - 1)
            return subscript(arr.name, (const(SINT, idx),), arr.elem, arr.values[idx], True, arr.access)
    if kind == "member" and env.struct_vars:
        var = rng.choice(list(env.struct_vars.values()))
        fld = rng.choice(var.stype.fields)
        # A narrow bit-field promotes to int (C11 6.3.1.1) even when its
        # declared base type is unsigned int. Model the promoted type so
        # later arithmetic matches the compiler.
        typ = SINT if fld.bitwidth is not None else fld.typ
        return member(var.name, fld.name, typ, fld.value, True)
    if kind == "deref" and env.pointers:
        ptr = rng.choice(list(env.pointers.values()))
        target = env.scalars[ptr.target]
        return deref(ptr.name, ptr.typ, target.value, True)
    if kind == "scalar":
        options = _readable_names(env)
        if options:
            nm, typ, value, pure = rng.choice(options)
            return name(typ, value, nm, pure)
    return _const_expr(rng, policy, env)


def _remember_cse(rng: Rng, policy: Policy, env: Env, expr: E) -> None:
    if not expr.pure or expr.kind == "const" or expr.node_count() > 6:
        return
    if expr.contains_kind("sub") and env.in_loop:
        # Loop subscripts are pure for uniform values, but duplicating them is
        # exactly the CSE policy; allow a short subscript expression.
        pass
    if rng.chance(policy.p_cse_record) and len(env.cse) < 10:
        env.cse.append(expr)


def _gen_expr(rng: Rng, policy: Policy, env: Env, depth: int, context: str) -> E:
    if depth >= policy.max_expr_depth or rng.chance(policy.p_leaf):
        return _gen_leaf(rng, policy, env)
    if rng.chance(0.3):
        context = rng.weighted(policy.context_weights)
    op = rng.choice(CONTEXTS[context])
    expr: E
    if op in {"neg", "~", "!"}:
        child = _gen_expr(rng, policy, env, depth + 1, context)
        unary_op = "-" if op == "neg" else op
        expr = apply_unary(unary_op, child)
    elif op == "?:":
        cond = _gen_expr(rng, policy, env, depth + 1, "logical")
        dest = _pick_type(rng, policy, bool_ok=False)
        then = _gen_expr(rng, policy, env, depth + 1, context)
        els = _gen_expr(rng, policy, env, depth + 1, context)
        from yarpgen.ub import safe_ternary

        expr = safe_ternary(cond, then, els, dest)
    else:
        left = _gen_expr(rng, policy, env, depth + 1, context)
        right = _gen_expr(rng, policy, env, depth + 1, context)
        expr = apply_binary(op, left, right)
    _remember_cse(rng, policy, env, expr)
    return expr


def _gen_decl(rng: Rng, policy: Policy, env: Env) -> StmtDecl:
    typ = _pick_type(rng, policy)
    init = fit_to_type(_gen_expr(rng, policy, env, 0, "any"), typ)
    nm = env.fresh("l_")
    env.locals[nm] = (typ, init.value)
    return StmtDecl(nm, typ, init)


def _pick_scalar_target(rng: Rng, policy: Policy, env: Env):
    options = []
    for nm, (typ, _value) in env.locals.items():
        options.append(("local", nm, typ, None))
    for slot in env.scalars.values():
        if slot.role in {"mixed", "output"}:
            options.append(("scalar", slot.name, slot.typ, None))
    for arr in env.arrays.values():
        if arr.role == "output" and len(arr.dims) == 1:
            options.append(("arr", arr.name, arr.elem, arr))
    if not options:
        # Only reached before the preamble has created outputs. Never used from
        # inside a conditional once globals exist.
        slot = Scalar(env.fresh("out_"), SINT, 0, "output")
        env.add_scalar(slot)
        options.append(("scalar", slot.name, slot.typ, None))
    return rng.choice(options)


def _gen_assign(rng: Rng, policy: Policy, env: Env) -> StmtAssign:
    kind, nm, typ, arr = _pick_scalar_target(rng, policy, env)
    expr = fit_to_type(_gen_expr(rng, policy, env, 0, rng.weighted(policy.context_weights)), typ)
    if kind == "arr":
        idx = rng.randint(0, arr.dims[0] - 1)
        arr.values[idx] = expr.value
        target = subscript(nm, (const(SINT, idx),), typ, expr.value, False, arr.access)
        return StmtAssign(target, expr)
    target = name(typ, expr.value, nm, False)
    if kind == "local":
        env.locals[nm] = (typ, expr.value)
    else:
        env.scalars[nm].value = expr.value
    return StmtAssign(target, expr)


def _gen_block(rng: Rng, policy: Policy, env: Env, depth: int, count: int | None = None) -> list:
    if count is None:
        hi = policy.scalar_stmts if depth == 0 else max(1, policy.scalar_stmts // 2)
        count = rng.randint(1, max(1, hi))
    body = []
    for _ in range(count):
        kind = rng.weighted(policy.stmt_weights)
        if kind == "if" and depth < policy.if_depth:
            body.append(_gen_if(rng, policy, env, depth))
        elif kind == "decl" or not _readable_names(env):
            body.append(_gen_decl(rng, policy, env))
        else:
            body.append(_gen_assign(rng, policy, env))
    return body


def _gen_if(rng: Rng, policy: Policy, env: Env, depth: int) -> StmtIf:
    cond = _gen_expr(rng, policy, env, 0, "logical")
    taken = cond.value != 0
    outer_locals = set(env.locals)
    snap = env.snapshot()
    then_body = _gen_block(rng, policy, env, depth + 1, count=rng.randint(1, 3))
    then_snap = env.snapshot()
    env.restore_values(snap)
    else_body = _gen_block(rng, policy, env, depth + 1, count=rng.randint(1, 3))
    env.restore_values(then_snap if taken else env.snapshot())
    # Declarations inside a branch are scoped to that branch.
    for nm in list(env.locals):
        if nm not in outer_locals:
            del env.locals[nm]
    return StmtIf(cond, then_body, else_body)


def _storage_for(rng: Rng, policy: Policy, lang: str, dims: tuple[int, ...]) -> tuple[str, str]:
    if lang != "cxx" or len(dims) != 1:
        return "c_array", "subscript"
    storage = rng.weighted(policy.cxx_storage_weights)
    access = "subscript"
    if storage in {"vector", "std_array"} and rng.chance(policy.p_at_access):
        access = "at"
    return storage, access


def _new_array(
    env: Env,
    rng: Rng,
    policy: Policy,
    lang: str,
    *,
    dims: tuple[int, ...],
    role: str,
    pattern: str,
    elem: IntType | None = None,
) -> ArrayVar:
    if elem is None:
        elem = UCHAR if pattern == "byte" else _pick_type(rng, policy, bool_ok=pattern != "byte")
    if pattern == "partition":
        even = _gen_const_value(rng, policy, env, elem)
        odd = _gen_const_value(rng, policy, env, elem)
        values = [even if i % 2 == 0 else odd for i in range(dims[0])]
    elif pattern == "zero":
        values = _fill(dims, 0 if elem.name != "bool" else 0)
    else:
        fill_v = _gen_const_value(rng, policy, env, elem)
        values = _fill(dims, fill_v)
    storage, access = _storage_for(rng, policy, lang, dims)
    arr = ArrayVar(
        env.fresh("a_"),
        elem,
        dims,
        values,
        role,
        storage,
        access,
        _copy_values(values),
    )
    env.arrays[arr.name] = arr
    return arr


def _bound(env: Env, rng: Rng, policy: Policy, value: int) -> Bound:
    if rng.chance(policy.p_opaque_bound):
        slot = Scalar(env.fresh("bnd_"), SINT, value, "input", const=True)
        env.add_scalar(slot)
        return Bound(value, slot.name)
    return Bound(value)


def _index(iv: str, concrete: int, offset: int) -> E:
    base = name(SINT, concrete, iv, True)
    if offset == 0:
        return base
    expr = safe_add(base, const(SINT, offset))
    if expr.value != concrete + offset:
        raise RuntimeError("index rewrite changed the address")
    return expr


def _scrub_cse(env: Env, banned: set[str]) -> None:
    if not banned:
        return
    env.cse = [expr for expr in env.cse if not (expr_names(expr) & banned)]


def _pragma(rng: Rng, policy: Policy) -> list[str]:
    if not rng.chance(policy.p_pragma):
        return []
    return [
        "#pragma clang loop vectorize(enable)",
        "#pragma clang loop interleave(enable)",
    ]


def _assign_1d(arr: ArrayVar, iv: str, ivs: list[int], expr: E, per_index: dict[int, int] | None = None) -> StmtAssign:
    target = subscript(arr.name, (_index(iv, ivs[0], 0),), arr.elem, expr.value, False, arr.access)
    for i in ivs:
        value = expr.value if per_index is None else per_index[i]
        _set_at(arr.values, (i,), value)
    return StmtAssign(target, expr)


def _loop_reads(arr: ArrayVar, iv: str, representative: int, offsets: tuple[int, ...]) -> list[E]:
    reads = []
    for off in offsets:
        # Representative element value: caller passes uniform arrays, so any index works.
        # Partition builds its own reads.
        idx = representative + off
        value = arr.values[idx] if len(arr.dims) == 1 else arr.values[idx]
        if isinstance(value, list):
            raise RuntimeError("1d read on a nested array")
        reads.append(subscript(arr.name, (_index(iv, representative, off),), arr.elem, value, True, arr.access))
    return reads


def _gen_loop(env: Env, rng: Rng, policy: Policy, lang: str, kind: str) -> tuple[object, str]:
    if kind == "fusion":
        return _gen_fusion(env, rng, policy, lang)
    if kind == "nest":
        return _gen_nest(env, rng, policy, lang)
    if kind == "byte":
        return _gen_byte(env, rng, policy)
    if kind == "stencil":
        return _gen_stencil(env, rng, policy, lang)
    if kind == "reduction":
        return _gen_reduction(env, rng, policy, lang)
    if kind == "partition":
        return _gen_partition(env, rng, policy, lang)
    return _gen_uniform(env, rng, policy, lang)


def _gen_uniform(env: Env, rng: Rng, policy: Policy, lang: str) -> tuple[StmtLoop, str]:
    start, end, step = 0, policy.array_len, 1 if rng.chance(0.75) else rng.choice([1, 2, 3])
    ivs = _iter_space(start, end, step)
    assert ivs
    src = _new_array(env, rng, policy, lang, dims=(end,), role="input", pattern="uniform")
    dst = _new_array(env, rng, policy, lang, dims=(end,), role="output", pattern="zero", elem=src.elem)
    iv = env.fresh("i_")
    saved_reads, saved_loop, saved_locals = env.reads, env.in_loop, dict(env.locals)
    env.in_loop = True
    env.reads = _loop_reads(src, iv, ivs[0], (0,))
    expr = fit_to_type(_gen_expr(rng, policy, env, 0, rng.weighted(policy.context_weights)), dst.elem)
    env.reads, env.in_loop, env.locals = saved_reads, saved_loop, saved_locals
    loop = StmtLoop(
        iv,
        _bound(env, rng, policy, start),
        _bound(env, rng, policy, end),
        _bound(env, rng, policy, step),
        [_assign_1d(dst, iv, ivs, expr)],
        _pragma(rng, policy),
        "uniform vectorizable",
    )
    _scrub_cse(env, {iv})
    return loop, f"uniform i in [{start}, {end}) step {step}"


def _gen_partition(env: Env, rng: Rng, policy: Policy, lang: str) -> tuple[StmtLoop, str]:
    end = policy.array_len + (policy.array_len % 2)
    if end < 4:
        end = 4
    ivs = _iter_space(0, end, 1)
    assert ivs
    src = _new_array(env, rng, policy, lang, dims=(end,), role="input", pattern="partition")
    dst = _new_array(env, rng, policy, lang, dims=(end,), role="output", pattern="zero", elem=src.elem)
    iv = env.fresh("i_")
    zero = env.scalars["in_zero"]

    def one_side(parity: int) -> E:
        saved_reads, saved_loop, saved_locals = env.reads, env.in_loop, dict(env.locals)
        env.in_loop = True
        rep = 0 if parity == 0 else 1
        env.reads = [
            subscript(
                src.name,
                (_index(iv, rep, 0),),
                src.elem,
                src.values[rep],
                True,
                src.access,
            )
        ]
        expr = fit_to_type(_gen_expr(rng, policy, env, 0, rng.weighted(policy.context_weights)), dst.elem)
        env.reads, env.in_loop, env.locals = saved_reads, saved_loop, saved_locals
        return expr

    even_e = one_side(0)
    odd_e = one_side(1)
    cond = apply_binary(
        "==",
        apply_binary("%", name(SINT, 0, iv, True), const(SINT, 2)),
        name(zero.typ, zero.value, zero.name, True),
    )
    from yarpgen.ub import safe_ternary

    merged = safe_ternary(cond, even_e, odd_e, dst.elem)
    per = {i: even_e.value if i % 2 == 0 else odd_e.value for i in ivs}
    loop = StmtLoop(
        iv,
        _bound(env, rng, policy, 0),
        _bound(env, rng, policy, end),
        Bound(1),
        [_assign_1d(dst, iv, ivs, merged, per)],
        _pragma(rng, policy),
        "even/odd partition",
    )
    _scrub_cse(env, {iv})
    return loop, f"partition i in [0, {end})"


def _gen_stencil(env: Env, rng: Rng, policy: Policy, lang: str) -> tuple[StmtLoop, str]:
    offsets = (-1, 0, 1) if rng.chance(0.7) else (-2, -1, 0, 1, 2)
    margin = max(abs(o) for o in offsets)
    length = max(policy.array_len, margin * 2 + 2)
    start, end = margin, length - margin
    ivs = _iter_space(start, end, 1)
    assert ivs
    src = _new_array(env, rng, policy, lang, dims=(length,), role="input", pattern="uniform")
    dst = _new_array(env, rng, policy, lang, dims=(length,), role="output", pattern="zero")
    iv = env.fresh("i_")
    parts = _loop_reads(src, iv, ivs[0], offsets)
    acc = parts[0]
    combine = rng.choice(["+", "^", "&", "|"])
    for part in parts[1:]:
        acc = apply_binary(combine, acc, part)
    expr = fit_to_type(acc, dst.elem)
    # Only the iterated indices are written; margins stay 0.
    target = subscript(dst.name, (_index(iv, ivs[0], 0),), dst.elem, expr.value, False, dst.access)
    for i in ivs:
        dst.values[i] = expr.value
    loop = StmtLoop(
        iv,
        _bound(env, rng, policy, start),
        _bound(env, rng, policy, end),
        Bound(1),
        [StmtAssign(target, expr)],
        _pragma(rng, policy),
        f"stencil offsets {offsets}",
    )
    _scrub_cse(env, {iv})
    return loop, f"stencil i in [{start}, {end}) offsets {list(offsets)}"


def _gen_reduction(env: Env, rng: Rng, policy: Policy, lang: str) -> tuple[StmtBlock, str]:
    end = policy.array_len
    ivs = _iter_space(0, end, 1)
    assert ivs
    src = _new_array(env, rng, policy, lang, dims=(end,), role="input", pattern="uniform")
    iv = env.fresh("i_")
    saved_reads, saved_loop, saved_locals = env.reads, env.in_loop, dict(env.locals)
    env.in_loop = True
    env.reads = _loop_reads(src, iv, ivs[0], (0,))
    elem_expr = fit_to_type(_gen_expr(rng, policy, env, 0, rng.weighted(policy.context_weights)), UINT)
    env.reads, env.in_loop, env.locals = saved_reads, saved_loop, saved_locals
    op = rng.choice(["+", "^", "&", "|"])
    acc = 0
    per = elem_expr.value
    for _ in ivs:
        if op == "+":
            acc = convert(acc + per, UINT)
        elif op == "^":
            acc = convert(acc ^ per, UINT)
        elif op == "&":
            acc = convert(acc & per, UINT)
        else:
            acc = convert(acc | per, UINT)
    red = env.fresh("red_")
    env.locals[red] = (UINT, acc)
    out = Scalar(env.fresh("out_"), UINT, acc, "output")
    env.add_scalar(out)
    from yarpgen.ir import binary as mk_binary

    update = StmtAssign(
        name(UINT, acc, red, False),
        mk_binary(op, name(UINT, 0, red, False), elem_expr, UINT, per),
    )
    loop = StmtLoop(iv, Bound(0), _bound(env, rng, policy, end), Bound(1), [update], _pragma(rng, policy), f"reduction {op}")
    block = StmtBlock(
        [
            StmtDecl(red, UINT, const(UINT, 0)),
            loop,
            StmtAssign(name(UINT, acc, out.name, False), name(UINT, acc, red, False)),
        ],
        note=f"reduction {op}",
    )
    # ``red`` is declared inside this block, so later statements must not see it.
    env.locals.pop(red, None)
    _scrub_cse(env, {iv, red})
    return block, f"reduction {op} over [0, {end})"


def _gen_byte(env: Env, rng: Rng, policy: Policy) -> tuple[StmtLoop, str]:
    end = policy.array_len
    ivs = _iter_space(0, end, 1)
    assert ivs
    kind = rng.choice(["copy", "set"])
    src = _new_array(env, rng, policy, "c", dims=(end,), role="input", pattern="uniform", elem=UCHAR)
    dst = _new_array(env, rng, policy, "c", dims=(end,), role="output", pattern="zero", elem=UCHAR)
    iv = env.fresh("i_")
    if kind == "copy":
        expr = subscript(src.name, (_index(iv, ivs[0], 0),), UCHAR, src.values[0], True, "subscript")
        for i in ivs:
            dst.values[i] = src.values[i]
    else:
        fill = _gen_const_value(rng, policy, env, UCHAR)
        expr = const(UCHAR, fill)
        for i in ivs:
            dst.values[i] = fill
    target = subscript(dst.name, (_index(iv, ivs[0], 0),), UCHAR, expr.value, False, "subscript")
    loop = StmtLoop(iv, Bound(0), _bound(env, rng, policy, end), Bound(1), [StmtAssign(target, expr)], [], f"byte {kind}")
    _scrub_cse(env, {iv})
    return loop, f"byte-{kind} [0, {end})"


def _gen_fusion(env: Env, rng: Rng, policy: Policy, lang: str) -> tuple[StmtBlock, str]:
    first, note1 = _gen_uniform(env, rng, policy, lang)
    # Force the second loop to share the iteration space of the first.
    second_src_end = first.end.value
    second_start = first.start.value
    second_step = first.step.value
    ivs = _iter_space(second_start, second_src_end, second_step)
    assert ivs
    src = _new_array(env, rng, policy, lang, dims=(max(second_src_end, 1),), role="input", pattern="uniform")
    dst = _new_array(env, rng, policy, lang, dims=(max(second_src_end, 1),), role="output", pattern="zero", elem=src.elem)
    iv = env.fresh("i_")
    saved_reads, saved_loop, saved_locals = env.reads, env.in_loop, dict(env.locals)
    env.in_loop = True
    env.reads = _loop_reads(src, iv, ivs[0], (0,))
    expr = fit_to_type(_gen_expr(rng, policy, env, 0, rng.weighted(policy.context_weights)), dst.elem)
    env.reads, env.in_loop, env.locals = saved_reads, saved_loop, saved_locals
    second = StmtLoop(
        iv,
        Bound(second_start, first.start.name),
        Bound(second_src_end, first.end.name),
        Bound(second_step, first.step.name),
        [_assign_1d(dst, iv, ivs, expr)],
        _pragma(rng, policy),
        "fusion partner",
    )
    _scrub_cse(env, {iv, first.iv})
    return StmtBlock([first, second], note="fusible sequence"), f"fusion ({note1})"


def _gen_nest(env: Env, rng: Rng, policy: Policy, lang: str) -> tuple[StmtLoop, str]:
    n = policy.nest_extent
    m = policy.nest_extent
    column = rng.chance(0.5)
    ivs_i = _iter_space(0, n, 1)
    ivs_j = _iter_space(0, m, 1)
    assert ivs_i and ivs_j
    in_dims = (m, n) if column else (n, m)
    src = _new_array(env, rng, policy, lang, dims=in_dims, role="input", pattern="uniform")
    dst = _new_array(env, rng, policy, lang, dims=(n, m), role="output", pattern="zero", elem=_pick_type(rng, policy, bool_ok=False))
    ivi = env.fresh("i_")
    ivj = env.fresh("j_")
    if column:
        # in[j][i], representative j=0,i=0. values[0][0] is the uniform fill.
        elem_value = src.values[0][0]
        read = subscript(
            src.name,
            (_index(ivj, 0, 0), _index(ivi, 0, 0)),
            src.elem,
            elem_value,
            True,
            "subscript",
        )
    else:
        elem_value = src.values[0][0]
        read = subscript(
            src.name,
            (_index(ivi, 0, 0), _index(ivj, 0, 0)),
            src.elem,
            elem_value,
            True,
            "subscript",
        )
    saved_reads, saved_loop, saved_locals = env.reads, env.in_loop, dict(env.locals)
    env.in_loop = True
    env.reads = [read]
    expr = fit_to_type(_gen_expr(rng, policy, env, 0, rng.weighted(policy.context_weights)), dst.elem)
    env.reads, env.in_loop, env.locals = saved_reads, saved_loop, saved_locals
    for i in ivs_i:
        for j in ivs_j:
            dst.values[i][j] = expr.value
    target = subscript(
        dst.name,
        (_index(ivi, 0, 0), _index(ivj, 0, 0)),
        dst.elem,
        expr.value,
        False,
        "subscript",
    )
    inner = StmtLoop(ivj, Bound(0), Bound(m), Bound(1), [StmtAssign(target, expr)], [], "inner")
    outer = StmtLoop(
        ivi,
        Bound(0),
        _bound(env, rng, policy, n),
        Bound(1),
        [inner],
        _pragma(rng, policy),
        "column-major" if column else "row-major",
    )
    _scrub_cse(env, {ivi, ivj})
    return outer, f"nest {n}x{m} {'column' if column else 'row'}"


def _build_globals_fixed(env: Env, rng: Rng, policy: Policy, lang: str) -> None:
    env.add_scalar(Scalar("in_zero", SINT, 0, "input", const=True))
    for _ in range(rng.randint(3, 7)):
        typ = _pick_type(rng, policy)
        env.add_scalar(
            Scalar(env.fresh("in_"), typ, _gen_const_value(rng, policy, env, typ), "input", const=rng.chance(0.4))
        )
    for _ in range(rng.randint(1, 2)):
        typ = _pick_type(rng, policy, bool_ok=False)
        env.add_scalar(Scalar(env.fresh("mix_"), typ, _gen_const_value(rng, policy, env, typ), "mixed"))
    for _ in range(rng.randint(2, 4)):
        typ = _pick_type(rng, policy, bool_ok=False)
        env.add_scalar(Scalar(env.fresh("out_"), typ, 0, "output"))
    for _ in range(rng.randint(0, 2)):
        typ = _pick_type(rng, policy, bool_ok=False)
        env.add_scalar(Scalar(env.fresh("p_"), typ, _gen_const_value(rng, policy, env, typ), "param"))
    _new_array(env, rng, policy, lang, dims=(4,), role="input", pattern="uniform")
    if rng.chance(0.85):
        _add_struct(env, rng, policy)
    inputs = [s for s in env.scalars.values() if s.role == "input" and not s.const]
    if inputs and rng.chance(0.75):
        target = rng.choice(inputs)
        ptr_name = env.fresh("ptr_")
        env.pointers[ptr_name] = PointerVar(ptr_name, target.name, target.typ)


def _add_struct(env: Env, rng: Rng, policy: Policy) -> None:
    from yarpgen.typesys import UINT as UNSIGNED

    fields = []
    for i in range(rng.randint(2, 4)):
        if rng.chance(0.5):
            base = SINT if rng.chance(0.5) else UNSIGNED
            width = rng.randint(1, 7)
            if base.signed:
                lo = -(1 << (width - 1))
                hi = (1 << (width - 1)) - 1
                fields.append(Field(f"f{i}", base, rng.randint(lo, hi), width))
            else:
                fields.append(Field(f"f{i}", base, rng.randint(0, (1 << width) - 1), width))
        else:
            typ = _pick_type(rng, policy)
            fields.append(Field(f"f{i}", typ, _gen_const_value(rng, policy, env, typ), None))
    stype = StructType(env.fresh("S_"), fields)
    env.structs.append(stype)
    var_name = env.fresh("st_")
    env.struct_vars[var_name] = StructVar(var_name, stype)


def _ensure_output(env: Env, rng: Rng, policy: Policy, body: list) -> None:
    has_output = any(s.role == "output" for s in env.scalars.values()) or any(
        a.role == "output" for a in env.arrays.values()
    )
    if has_output:
        return
    typ = SINT
    src = next(s for s in env.scalars.values() if s.role == "input")
    slot = Scalar(env.fresh("out_"), typ, convert(src.value, typ) if typ.fits(src.value) else 0, "output")
    if not typ.fits(src.value):
        expr = fit_to_type(name(src.typ, src.value, src.name, True), typ)
        slot.value = expr.value
    else:
        expr = fit_to_type(name(src.typ, src.value, src.name, True), typ)
        slot.value = expr.value
    env.add_scalar(slot)
    body.append(StmtAssign(name(typ, slot.value, slot.name, False), expr))


def _hash_items(env: Env) -> list[HashItem]:
    items: list[HashItem] = []
    for slot in sorted(env.scalars.values(), key=lambda s: s.name):
        if slot.role in {"output", "mixed"}:
            items.append(HashItem(slot.name, slot.value, slot.typ))
    for arr in sorted(env.arrays.values(), key=lambda a: a.name):
        if arr.role != "output":
            continue
        for index in _row_major(arr.dims):
            literal = "".join(f"[{i}]" for i in index)
            items.append(HashItem(f"{arr.name}{literal}", _value_at(arr.values, index), arr.elem))
    return items


def _checksum(items: list[HashItem]) -> int:
    state = 0
    for item in items:
        state = mix_hash(state, item.value, item.typ)
    return state


def _skeleton(notes: list[str], policy: Policy) -> str:
    lines = [
        f"profile={policy.summary.get('profile')} depth={policy.max_expr_depth} "
        f"scalar_stmts={policy.scalar_stmts} array_len={policy.array_len}",
        "scalar block",
        *notes,
        "scalar block",
    ]
    return "\n".join(lines)


def generate(
    seed: int,
    lang: str = "c",
    profile: str = "smoke",
    flips: dict[int, int] | None = None,
    force_loops: list[str] | None = None,
) -> Program:
    if lang not in {"c", "cxx"}:
        raise ValueError(lang)
    if profile not in {"tiny", "smoke", "campaign"}:
        raise ValueError(profile)
    rng = Rng(seed, flips)
    try:
        return _generate(rng, lang, profile, force_loops)
    except Exception as exc:  # pragma: no cover - context for campaign logs
        raise RuntimeError(f"generation failed for seed {seed} ({lang}, {profile})") from exc


def _generate(rng: Rng, lang: str, profile: str, force_loops: list[str] | None) -> Program:
    policy = shuffle_policy(rng, profile)
    env = Env()
    _build_globals_fixed(env, rng, policy, lang)
    body: list = []
    notes: list[str] = []
    prefix_n = policy.scalar_stmts if profile != "tiny" else 3
    body.extend(_gen_block(rng, policy, env, 0, count=prefix_n))
    if force_loops is not None:
        kinds = list(force_loops)
    elif profile == "tiny":
        kinds = []
    else:
        kinds = [rng.weighted(policy.loop_weights) for _ in range(rng.randint(1, 3 if profile == "smoke" else 4))]
    for kind in kinds:
        stmt, note = _gen_loop(env, rng, policy, lang, kind)
        body.append(stmt)
        notes.append(note)
    if profile != "tiny":
        body.extend(_gen_block(rng, policy, env, 0, count=max(2, policy.scalar_stmts // 3)))
    _ensure_output(env, rng, policy, body)
    used = stmt_names(body)
    params = [s for s in env.scalars.values() if s.role == "param" and s.name in used]
    # Parameters that were never read are not part of the signature.
    items = _hash_items(env)
    if not items:
        raise RuntimeError("program has nothing to hash")
    return Program(
        lang=lang,
        seed=rng.seed,
        profile=profile,
        structs=list(env.structs),
        struct_vars=list(env.struct_vars.values()),
        scalars=[s for s in env.scalars.values() if s.role != "param"],
        arrays=list(env.arrays.values()),
        pointers=list(env.pointers.values()),
        params=params,
        body=body,
        hash_items=items,
        expected_checksum=_checksum(items),
        skeleton=_skeleton(notes, policy),
        policy_summary=dict(policy.summary),
        choices=list(rng.log),
    )

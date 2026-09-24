"""Generation policies and parameter shuffling.

OOPSLA 2020: distributions are themselves randomized per test (swarm-style),
and regions of an expression are biased toward operator contexts that fire
scalar optimizations. PLDI 2023: loop idioms (fusion, nests, stencils,
reductions, byte loops, vectorizable loops, even/odd partitions) are first-class
choices rather than accidents of independent draws.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from yarpgen.rng import Rng
from yarpgen.typesys import ALL_TYPES, IntType


CONTEXTS: dict[str, tuple[str, ...]] = {
    "any": (
        "+", "-", "*", "/", "%", "<<", ">>", "&", "|", "^",
        "~", "!", "neg", "&&", "||", "<", ">", "<=", ">=", "==", "!=", "?:",
    ),
    "additive": ("+", "-", "neg"),
    "bitwise": ("~", "&", "|", "^"),
    "logical": ("!", "&&", "||", "==", "!=", "<", ">", "<=", ">="),
    "multiplicative": ("*", "/", "%"),
    "bitwise_shift": ("~", "&", "|", "^", "<<", ">>"),
    "add_mul": ("+", "-", "neg", "*", "/", "%"),
}

LOOP_KINDS = (
    "uniform",
    "partition",
    "stencil",
    "reduction",
    "byte",
    "nest",
    "fusion",
)


@dataclass
class Policy:
    max_expr_depth: int
    scalar_stmts: int
    if_depth: int
    p_leaf: float
    p_cse_record: float
    p_cse_use: float
    p_opaque_bound: float
    p_pragma: float
    p_at_access: float
    type_weights: list[tuple[float, IntType]]
    context_weights: list[tuple[float, str]]
    const_weights: list[tuple[float, str]]
    leaf_weights: list[tuple[float, str]]
    stmt_weights: list[tuple[float, str]]
    loop_weights: list[tuple[float, str]]
    cxx_storage_weights: list[tuple[float, str]]
    array_len: int
    nest_extent: int
    summary: dict = field(default_factory=dict)


def _weights(rng: Rng, labels: list) -> list[tuple[float, object]]:
    raw = [rng.randint(1, 100) for _ in labels]
    total = float(sum(raw))
    return [(w / total, label) for w, label in zip(raw, labels)]


def shuffle_policy(rng: Rng, profile: str) -> Policy:
    if profile == "tiny":
        depth, stmts, length = 2, 4, 4
    elif profile == "campaign":
        depth = rng.randint(4, 7)
        stmts = rng.randint(12, 28)
        length = rng.randint(12, 24)
    else:
        depth = rng.randint(3, 5)
        stmts = rng.randint(6, 14)
        length = rng.randint(8, 16)
    types = [t for t in ALL_TYPES]
    policy = Policy(
        max_expr_depth=depth,
        scalar_stmts=stmts,
        if_depth=rng.randint(1, 3),
        p_leaf=rng.randint(15, 45) / 100,
        p_cse_record=rng.randint(20, 70) / 100,
        p_cse_use=rng.randint(10, 60) / 100,
        p_opaque_bound=rng.randint(40, 90) / 100,
        p_pragma=rng.randint(20, 80) / 100,
        p_at_access=rng.randint(10, 60) / 100,
        type_weights=_weights(rng, types),  # type: ignore[arg-type]
        context_weights=_weights(rng, list(CONTEXTS)),  # type: ignore[arg-type]
        const_weights=_weights(rng, ["small", "extreme", "bitblock", "reuse", "any"]),  # type: ignore[arg-type]
        leaf_weights=_weights(
            rng,
            ["const", "scalar", "array", "member", "deref", "cse"],
        ),  # type: ignore[arg-type]
        stmt_weights=_weights(rng, ["decl", "assign", "if"]),  # type: ignore[arg-type]
        loop_weights=_weights(rng, list(LOOP_KINDS)),  # type: ignore[arg-type]
        cxx_storage_weights=_weights(rng, ["c_array", "std_array", "vector", "valarray"]),  # type: ignore[arg-type]
        array_len=length,
        nest_extent=rng.randint(3, 6),
    )
    policy.summary = {
        "profile": profile,
        "max_expr_depth": policy.max_expr_depth,
        "scalar_stmts": policy.scalar_stmts,
        "array_len": policy.array_len,
        "p_leaf": policy.p_leaf,
        "p_cse_use": policy.p_cse_use,
        "p_opaque_bound": policy.p_opaque_bound,
    }
    return policy

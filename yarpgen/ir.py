"""Small expression and statement IR.

Nodes remember the concrete value of every subexpression so undefined behavior
can be rewritten locally, which is the static analysis from the OOPSLA 2020 paper.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from yarpgen.typesys import IntType, SINT


@dataclass
class E:
    kind: str
    typ: IntType
    value: int
    pure: bool
    op: str = ""
    kids: tuple["E", ...] = ()
    name: str = ""
    field_name: str = ""

    def node_count(self) -> int:
        return 1 + sum(k.node_count() for k in self.kids)

    def contains_kind(self, kind: str) -> bool:
        if self.kind == kind:
            return True
        return any(k.contains_kind(kind) for k in self.kids)


def const(typ: IntType, value: int) -> E:
    return E("const", typ, value, True)


def name(typ: IntType, value: int, nm: str, pure: bool) -> E:
    return E("name", typ, value, pure, name=nm)


def unary(op: str, kid: E, typ: IntType, value: int) -> E:
    return E("unary", typ, value, kid.pure, op=op, kids=(kid,))


def binary(op: str, left: E, right: E, typ: IntType, value: int) -> E:
    return E("binary", typ, value, left.pure and right.pure, op=op, kids=(left, right))


def cast(kid: E, typ: IntType, value: int) -> E:
    return E("cast", typ, value, kid.pure, kids=(kid,))


def ternary(cond: E, then: E, els: E, typ: IntType, value: int) -> E:
    return E(
        "ternary",
        typ,
        value,
        cond.pure and then.pure and els.pure,
        kids=(cond, then, els),
    )


def subscript(arr: str, indices: tuple[E, ...], typ: IntType, value: int, pure: bool, access: str) -> E:
    return E("sub", typ, value, pure, op=access, kids=indices, name=arr)


def member(base: str, field_name: str, typ: IntType, value: int, pure: bool) -> E:
    return E("member", typ, value, pure, name=base, field_name=field_name)


def deref(ptr: str, typ: IntType, value: int, pure: bool) -> E:
    return E("deref", typ, value, pure, name=ptr)


@dataclass
class Bound:
    """Loop bound: a literal or the name of an immutable input."""

    value: int
    name: str | None = None  # opaque input, when set

    def expr(self) -> E:
        if self.name is None:
            return const(SINT, self.value)
        return name(SINT, self.value, self.name, True)


@dataclass
class StmtDecl:
    name: str
    typ: IntType
    init: E


@dataclass
class StmtAssign:
    target: E  # name, sub, or member
    expr: E


@dataclass
class StmtIf:
    cond: E
    then_body: list
    else_body: list


@dataclass
class StmtLoop:
    iv: str
    start: Bound
    end: Bound
    step: Bound
    body: list
    pragmas: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class StmtBlock:
    body: list
    note: str = ""


def walk_exprs(stmts: list):
    for stmt in stmts:
        if isinstance(stmt, StmtDecl):
            yield stmt.init
        elif isinstance(stmt, StmtAssign):
            yield stmt.target
            yield stmt.expr
        elif isinstance(stmt, StmtIf):
            yield stmt.cond
            yield from walk_exprs(stmt.then_body)
            yield from walk_exprs(stmt.else_body)
        elif isinstance(stmt, StmtLoop):
            yield from walk_exprs(stmt.body)
        elif isinstance(stmt, StmtBlock):
            yield from walk_exprs(stmt.body)


def expr_names(expr: E) -> set[str]:
    found = set()
    if expr.kind in {"name", "sub", "member", "deref"} and expr.name:
        found.add(expr.name)
    for kid in expr.kids:
        found |= expr_names(kid)
    return found


def stmt_names(stmts: list) -> set[str]:
    found: set[str] = set()
    for stmt in stmts:
        if isinstance(stmt, StmtDecl):
            found |= expr_names(stmt.init)
        elif isinstance(stmt, StmtAssign):
            found |= expr_names(stmt.target)
            found |= expr_names(stmt.expr)
        elif isinstance(stmt, StmtIf):
            found |= expr_names(stmt.cond)
            found |= stmt_names(stmt.then_body)
            found |= stmt_names(stmt.else_body)
        elif isinstance(stmt, StmtLoop):
            for bound in (stmt.start, stmt.end, stmt.step):
                if bound.name:
                    found.add(bound.name)
            found |= stmt_names(stmt.body)
        elif isinstance(stmt, StmtBlock):
            found |= stmt_names(stmt.body)
    return found

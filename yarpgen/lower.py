"""Lower the IR to a separately compiled C or C++ test.

The driver owns every global initializer. The function translation unit only
sees declarations, so the compiler under test cannot constant-fold input
values into the code it optimizes (OOPSLA 2020, separate compilation).
"""

from __future__ import annotations

from pathlib import Path

from yarpgen.generate import ArrayVar, Program, Scalar
from yarpgen.ir import Bound, E, StmtAssign, StmtBlock, StmtDecl, StmtIf, StmtLoop
from yarpgen.typesys import IntType


def _unsigned_name(typ: IntType) -> str:
    return {
        "bool": "unsigned int",
        "signed char": "unsigned char",
        "unsigned char": "unsigned char",
        "short": "unsigned short",
        "unsigned short": "unsigned short",
        "int": "unsigned int",
        "unsigned int": "unsigned int",
        "long": "unsigned long",
        "unsigned long": "unsigned long",
        "long long": "unsigned long long",
        "unsigned long long": "unsigned long long",
    }[typ.name]


def render_value(value: int, typ: IntType, lang: str) -> str:
    if typ.name == "bool":
        if lang == "cxx":
            return "true" if value else "false"
        return "((_Bool)1)" if value else "((_Bool)0)"
    c_name = typ.c_name(lang)
    if not typ.signed:
        suffix = "U" if typ.bits <= 32 else ("UL" if typ.rank == 4 else "ULL")
        return f"(({c_name}){value}{suffix})"
    if value == typ.minv() and typ.bits >= 32:
        suffix = "" if typ.bits == 32 else ("L" if typ.rank == 4 else "LL")
        return f"(({c_name})((-{(typ.maxv())}{suffix}) - 1))"
    suffix = ""
    if typ.rank == 4:
        suffix = "L"
    elif typ.rank == 5:
        suffix = "LL"
    if value < 0:
        return f"(({c_name})({value}{suffix}))"
    return f"(({c_name}){value}{suffix})"


def render_expr(expr: E, lang: str) -> str:
    if expr.kind == "const":
        return render_value(expr.value, expr.typ, lang)
    if expr.kind == "name":
        return expr.name
    if expr.kind == "unary":
        return f"({expr.op}{render_expr(expr.kids[0], lang)})"
    if expr.kind == "binary":
        return f"({render_expr(expr.kids[0], lang)} {expr.op} {render_expr(expr.kids[1], lang)})"
    if expr.kind == "cast":
        return f"(({expr.typ.c_name(lang)}){render_expr(expr.kids[0], lang)})"
    if expr.kind == "ternary":
        cond, then, els = expr.kids
        return f"({render_expr(cond, lang)} ? {render_expr(then, lang)} : {render_expr(els, lang)})"
    if expr.kind == "sub":
        if expr.op == "at" and len(expr.kids) == 1:
            return f"({expr.name}.at({render_expr(expr.kids[0], lang)}))"
        text = expr.name
        for kid in expr.kids:
            text += f"[{render_expr(kid, lang)}]"
        return f"({text})"
    if expr.kind == "member":
        return f"({expr.name}.{expr.field_name})"
    if expr.kind == "deref":
        return f"(*{expr.name})"
    raise ValueError(expr.kind)


def _render_bound(bound: Bound) -> str:
    return bound.name if bound.name is not None else str(int(bound.value))


def _render_stmts(stmts: list, lang: str, indent: int) -> str:
    chunks = []
    pad = "    " * indent
    for stmt in stmts:
        if isinstance(stmt, StmtDecl):
            chunks.append(
                f"{pad}{stmt.typ.c_name(lang)} {stmt.name} = {render_expr(stmt.init, lang)};\n"
            )
        elif isinstance(stmt, StmtAssign):
            chunks.append(
                f"{pad}{render_expr(stmt.target, lang)} = {render_expr(stmt.expr, lang)};\n"
            )
        elif isinstance(stmt, StmtIf):
            chunks.append(f"{pad}if ({render_expr(stmt.cond, lang)}) {{\n")
            chunks.append(_render_stmts(stmt.then_body, lang, indent + 1))
            chunks.append(f"{pad}}} else {{\n")
            chunks.append(_render_stmts(stmt.else_body, lang, indent + 1))
            chunks.append(f"{pad}}}\n")
        elif isinstance(stmt, StmtLoop):
            if stmt.note:
                chunks.append(f"{pad}/* {stmt.note} */\n")
            for pragma in stmt.pragmas:
                chunks.append(f"{pad}{pragma}\n")
            chunks.append(
                f"{pad}for (int {stmt.iv} = {_render_bound(stmt.start)}; "
                f"{stmt.iv} < {_render_bound(stmt.end)}; "
                f"{stmt.iv} = {stmt.iv} + {_render_bound(stmt.step)}) {{\n"
            )
            chunks.append(_render_stmts(stmt.body, lang, indent + 1))
            chunks.append(f"{pad}}}\n")
        elif isinstance(stmt, StmtBlock):
            if stmt.note:
                chunks.append(f"{pad}/* {stmt.note} */\n")
            chunks.append(f"{pad}{{\n")
            chunks.append(_render_stmts(stmt.body, lang, indent + 1))
            chunks.append(f"{pad}}}\n")
        else:
            raise TypeError(type(stmt))
    return "".join(chunks)


def _needed_headers(program: Program) -> list[str]:
    headers = []
    if program.lang == "cxx":
        storages = {arr.storage for arr in program.arrays}
        if "vector" in storages:
            headers.append("#include <vector>")
        if "std_array" in storages:
            headers.append("#include <array>")
        if "valarray" in storages:
            headers.append("#include <valarray>")
    return headers


def _struct_defs(program: Program) -> str:
    parts = []
    for stype in program.structs:
        fields = []
        for fld in stype.fields:
            if fld.bitwidth is None:
                fields.append(f"    {fld.typ.c_name(program.lang)} {fld.name};")
            else:
                base = "int" if fld.typ.signed else "unsigned int"
                fields.append(f"    {base} {fld.name} : {fld.bitwidth};")
        body = "\n".join(fields)
        parts.append(f"typedef struct {stype.name} {{\n{body}\n}} {stype.name};")
    return "\n\n".join(parts)


def _scalar_decl(slot: Scalar, lang: str) -> str:
    const = "const " if slot.const else ""
    return f"extern {const}{slot.typ.c_name(lang)} {slot.name};"


def _array_decl(arr: ArrayVar, lang: str) -> str:
    elem = arr.elem.c_name(lang)
    if arr.storage == "c_array":
        dims = "".join(f"[{n}]" for n in arr.dims)
        return f"extern {elem} {arr.name}{dims};"
    if arr.storage == "std_array":
        return f"extern std::array<{elem}, {arr.dims[0]}> {arr.name};"
    if arr.storage == "vector":
        return f"extern std::vector<{elem}> {arr.name};"
    if arr.storage == "valarray":
        return f"extern std::valarray<{elem}> {arr.name};"
    raise ValueError(arr.storage)


def _braces(values, elem: IntType, lang: str) -> str:
    if values and isinstance(values[0], list):
        return "{" + ", ".join(_braces(v, elem, lang) for v in values) + "}"
    return "{" + ", ".join(render_value(v, elem, lang) for v in values) + "}"


def _array_def(arr: ArrayVar, lang: str) -> str:
    elem = arr.elem.c_name(lang)
    init = _braces(arr.init_values, arr.elem, lang)
    if arr.storage == "c_array":
        dims = "".join(f"[{n}]" for n in arr.dims)
        return f"{elem} {arr.name}{dims} = {init};"
    rendered = ", ".join(render_value(v, arr.elem, lang) for v in arr.init_values)
    if arr.storage == "std_array":
        return f"std::array<{elem}, {arr.dims[0]}> {arr.name} = {{{rendered}}};"
    if arr.storage == "vector":
        return f"std::vector<{elem}> {arr.name} = {{{rendered}}};"
    if arr.storage == "valarray":
        return f"std::valarray<{elem}> {arr.name} = {{{rendered}}};"
    raise ValueError(arr.storage)


def _scalar_def(slot: Scalar, lang: str) -> str:
    const = "const " if slot.const else ""
    # C++ gives namespace-scope const internal linkage unless the definition
    # is explicitly extern. C does the opposite: a file-scope const already
    # has external linkage, and `extern` plus an initializer warns.
    linkage = "extern " if slot.const and lang == "cxx" else ""
    init = 0 if slot.init is None else slot.init
    return f"{linkage}{const}{slot.typ.c_name(lang)} {slot.name} = {render_value(init, slot.typ, lang)};"


def _proto(program: Program) -> str:
    if not program.params:
        return "void test_func(void);"
    args = ", ".join(f"{p.typ.c_name(program.lang)} {p.name}" for p in program.params)
    return f"void test_func({args});"


def _call(program: Program) -> str:
    if not program.params:
        return "test_func();"
    args = ", ".join(render_value(p.value, p.typ, program.lang) for p in program.params)
    return f"test_func({args});"


def _mix_stmt(expr: str, typ: IntType) -> str:
    if typ.name == "bool":
        return f"    yarp_mix((unsigned long long)(({expr}) ? 1ULL : 0ULL));"
    uname = _unsigned_name(typ)
    return f"    yarp_mix((unsigned long long)({uname})({expr}));"


def render_files(program: Program) -> dict[str, str]:
    lang = program.lang
    header_name = "test.hpp" if lang == "cxx" else "test.h"
    func_name = "func.cpp" if lang == "cxx" else "func.c"
    driver_name = "driver.cpp" if lang == "cxx" else "driver.c"
    guard = "YARPGEN_TEST_H"
    includes = "\n".join(_needed_headers(program))
    structs = _struct_defs(program)
    decls = []
    for slot in program.scalars:
        decls.append(_scalar_decl(slot, lang))
    for arr in program.arrays:
        decls.append(_array_decl(arr, lang))
    for ptr in program.pointers:
        decls.append(f"extern {ptr.typ.c_name(lang)} *{ptr.name};")
    for var in program.struct_vars:
        decls.append(f"extern {var.stype.name} {var.name};")
    header = "\n".join(
        [
            f"#ifndef {guard}",
            f"#define {guard}",
            includes,
            structs,
            "",
            "\n".join(decls),
            "",
            _proto(program),
            "",
            f"#endif /* {guard} */",
            "",
        ]
    )
    skeleton = "\n".join(f" * {line}" if line else " *" for line in program.skeleton.splitlines())
    func = "\n".join(
        [
            f"#include \"{header_name}\"",
            "",
            "/*",
            f" * YARPGen seed {program.seed} lang {lang} profile {program.profile}",
            f" * expected checksum {program.expected_checksum}",
            " *",
            skeleton,
            " */",
            "",
            _proto(program)[:-1],
            "{",
            "/* YARPGEN_BODY_BEGIN */",
            _render_stmts(program.body, lang, 1).rstrip(),
            "/* YARPGEN_BODY_END */",
            "}",
            "",
        ]
    )
    defs = []
    for slot in program.scalars:
        defs.append(_scalar_def(slot, lang))
    for arr in program.arrays:
        defs.append(_array_def(arr, lang))
    for ptr in program.pointers:
        defs.append(f"{ptr.typ.c_name(lang)} *{ptr.name} = &{ptr.target};")
    for var in program.struct_vars:
        inits = ", ".join(render_value(fld.value, fld.typ, lang) for fld in var.stype.fields)
        defs.append(f"{var.stype.name} {var.name} = {{{inits}}};")
    mixes = "\n".join(_mix_stmt(item.expr, item.typ) for item in program.hash_items)
    stdio = "#include <cstdio>" if lang == "cxx" else "#include <stdio.h>"
    printer = "std::printf" if lang == "cxx" else "printf"
    driver = "\n".join(
        [
            f"#include \"{header_name}\"",
            stdio,
            "",
            "\n".join(defs),
            "",
            "static unsigned long long yarp_hash;",
            "static void yarp_mix(unsigned long long v) {",
            "    yarp_hash = yarp_hash * 0x9E3779B185EBCA87ULL + v;",
            "}",
            "",
            "int main(void) {",
            f"    {_call(program)}",
            mixes,
            f"    {printer}(\"%llu\\n\", yarp_hash);",
            "    return 0;",
            "}",
            "",
        ]
    )
    return {header_name: header, func_name: func, driver_name: driver}


def write_program(program: Program, out_dir: Path) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    files = render_files(program)
    for name, text in files.items():
        (out_dir / name).write_text(text)
    return files

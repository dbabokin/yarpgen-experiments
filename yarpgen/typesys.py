"""C/C++ integer types, promotions, and conversions (LP64, two's complement)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IntType:
    name: str
    bits: int
    signed: bool
    rank: int

    def minv(self) -> int:
        if self.name == "bool":
            return 0
        if not self.signed:
            return 0
        return -(1 << (self.bits - 1))

    def maxv(self) -> int:
        if self.name == "bool":
            return 1
        if not self.signed:
            return (1 << self.bits) - 1
        return (1 << (self.bits - 1)) - 1

    def fits(self, value: int) -> bool:
        return self.minv() <= value <= self.maxv()

    def c_name(self, lang: str) -> str:
        if self.name == "bool":
            return "bool" if lang == "cxx" else "_Bool"
        return self.name


BOOL = IntType("bool", 1, False, 0)
SCHAR = IntType("signed char", 8, True, 1)
UCHAR = IntType("unsigned char", 8, False, 1)
SSHORT = IntType("short", 16, True, 2)
USHORT = IntType("unsigned short", 16, False, 2)
SINT = IntType("int", 32, True, 3)
UINT = IntType("unsigned int", 32, False, 3)
SLONG = IntType("long", 64, True, 4)
ULONG = IntType("unsigned long", 64, False, 4)
SLL = IntType("long long", 64, True, 5)
ULL = IntType("unsigned long long", 64, False, 5)

ALL_TYPES: tuple[IntType, ...] = (
    BOOL,
    SCHAR,
    UCHAR,
    SSHORT,
    USHORT,
    SINT,
    UINT,
    SLONG,
    ULONG,
    SLL,
    ULL,
)

UNSIGNED_OF = {
    SINT: UINT,
    SLONG: ULONG,
    SLL: ULL,
    SCHAR: UCHAR,
    SSHORT: USHORT,
    BOOL: BOOL,
    UCHAR: UCHAR,
    USHORT: USHORT,
    UINT: UINT,
    ULONG: ULONG,
    ULL: ULL,
}


def promote(typ: IntType) -> IntType:
    """Integer promotions (C11 6.3.1.1). All sub-int types we emit fit in int."""
    if typ.rank < SINT.rank:
        return SINT
    return typ


def unsigned_variant(typ: IntType) -> IntType:
    return UNSIGNED_OF[typ]


def uac(a: IntType, b: IntType) -> IntType:
    """Usual arithmetic conversions (C11 6.3.1.8), integers only."""
    a, b = promote(a), promote(b)
    if a == b:
        return a
    if a.signed == b.signed:
        return a if a.rank >= b.rank else b
    unsigned, signed = (a, b) if not a.signed else (b, a)
    if unsigned.rank >= signed.rank:
        return unsigned
    if signed.fits(unsigned.maxv()):
        return signed
    return unsigned_variant(signed)


def convert(value: int, typ: IntType) -> int:
    """Convert a mathematical integer to ``typ`` the way C does when defined.

    Unsigned conversion is modulo 2^bits. Signed conversion is returned unchanged
    when the value fits. Out-of-range signed conversion is implementation-defined;
    callers that emit source must not rely on it. The two's-complement wrap below
    is only a fallback for internal bookkeeping.
    """
    if typ.name == "bool":
        return 1 if value != 0 else 0
    if not typ.signed:
        return value & ((1 << typ.bits) - 1)
    if typ.fits(value):
        return value
    mod = 1 << typ.bits
    wrapped = value % mod
    if wrapped >= (1 << (typ.bits - 1)):
        wrapped -= mod
    return wrapped


def c_div(a: int, b: int) -> int:
    """Truncating division, matching C99 and later."""
    if b == 0:
        raise ZeroDivisionError
    quot = abs(a) // abs(b)
    if (a < 0) ^ (b < 0):
        quot = -quot
    return quot


def c_mod(a: int, b: int) -> int:
    return a - c_div(a, b) * b


def mix_hash(state: int, value: int, typ: IntType) -> int:
    """Same mix the generated driver uses: zero-extend the object representation."""
    bits = value & ((1 << max(typ.bits, 1)) - 1) if typ.name != "bool" else (1 if value else 0)
    if typ.name == "bool":
        bits = 1 if value else 0
    state = (state * 0x9E3779B185EBCA87 + bits) & ((1 << 64) - 1)
    return state

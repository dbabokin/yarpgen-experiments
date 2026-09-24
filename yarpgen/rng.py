"""Seeded RNG that records every draw.

The log is the choice sequence from Livinskii's thesis (chapter 4): one draw can
be replaced and the rest of the stream stays aligned, which is enough to mutate
a generated program without a coverage oracle.
"""

from __future__ import annotations

import random
from typing import Sequence, TypeVar

T = TypeVar("T")


class Rng:
    def __init__(self, seed: int, flips: dict[int, int] | None = None) -> None:
        self.seed = seed
        self._r = random.Random(seed)
        self._flips = dict(flips or {})
        self._i = 0
        self.log: list[int] = []

    def randint(self, a: int, b: int) -> int:
        if a > b:
            a, b = b, a
        base = a if a == b else self._r.randint(a, b)
        idx = self._i
        value = base
        if idx in self._flips:
            flipped = self._flips[idx]
            if a <= flipped <= b:
                value = flipped
        self._i += 1
        self.log.append(value)
        return value

    def chance(self, probability: float) -> bool:
        # probability in [0, 1]. A single draw keeps the choice sequence uniform.
        threshold = max(0, min(1000, int(round(probability * 1000))))
        return self.randint(0, 999) < threshold

    def choice(self, items: Sequence[T]) -> T:
        if not items:
            raise ValueError("choice from empty sequence")
        return items[self.randint(0, len(items) - 1)]

    def weighted(self, items: Sequence[tuple[float, T]]) -> T:
        if not items:
            raise ValueError("weighted choice from empty sequence")
        weights = [max(0.0, w) for w, _ in items]
        total = sum(weights)
        if total <= 0:
            return items[0][1]
        # Scale to an integer sample so the decision is one logged draw.
        scale = 10000
        buckets = [max(1, int(w / total * scale)) for w in weights]
        # Fix rounding so the buckets are non-empty and cover a known range.
        pick = self.randint(0, sum(buckets) - 1)
        acc = 0
        for bucket, (_, value) in zip(buckets, items):
            acc += bucket
            if pick < acc:
                return value
        return items[-1][1]

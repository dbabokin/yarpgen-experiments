"""Generate one or more tests.

    python3 -m yarpgen --seed 1 --lang c --profile smoke --out /tmp/yg
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from yarpgen.generate import generate
from yarpgen.lower import write_program


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate a UB-free C or C++ program")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--lang", choices=["c", "cxx"], default="c")
    parser.add_argument("--profile", choices=["tiny", "smoke", "campaign"], default="smoke")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument(
        "--loops",
        default="",
        help="Comma-separated loop kinds to force (uniform,partition,stencil,reduction,byte,nest,fusion)",
    )
    parser.add_argument("--save-choices", action="store_true")
    args = parser.parse_args(argv)
    force = [part for part in args.loops.split(",") if part] or None
    for offset in range(args.count):
        seed = args.seed + offset
        program = generate(seed, args.lang, args.profile, force_loops=force)
        dest = args.out if args.count == 1 else args.out / f"seed_{seed}"
        write_program(program, dest)
        meta = {
            "seed": program.seed,
            "lang": program.lang,
            "profile": program.profile,
            "expected_checksum": program.expected_checksum,
            "skeleton": program.skeleton,
            "policy": program.policy_summary,
        }
        (dest / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        if args.save_choices:
            (dest / "choices.json").write_text(json.dumps(program.choices) + "\n")
        print(f"seed={seed} lang={args.lang} checksum={program.expected_checksum} dir={dest}")


if __name__ == "__main__":
    main()

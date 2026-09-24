"""Failure storage keeps interesting cases and drops successes."""

import tempfile
import unittest
from pathlib import Path

from yarpgen.campaign import Outcome, load_config, run_campaign, store_failure


class CampaignStoreTest(unittest.TestCase):
    def test_success_is_not_stored(self):
        store = Path(tempfile.mkdtemp(prefix="yarpgen-store-"))
        case = store / "tmp-case"
        case.mkdir()
        (case / "func.c").write_text("int x;\n")
        dest = store_failure(store, case, Outcome("ok", 1, "c", "ok"), {"max_stored_per_fingerprint": 1})
        self.assertIsNone(dest)
        self.assertFalse((store / "cases").exists())
        self.assertFalse((store / "index.jsonl").exists())

    def test_failure_is_stored_once_per_ice_fingerprint(self):
        store = Path(tempfile.mkdtemp(prefix="yarpgen-store-"))
        cfg = {"max_stored_per_fingerprint": 1}
        for seed in (1, 2):
            case = store / f"src{seed}"
            case.mkdir()
            (case / "func.c").write_text(f"/* seed {seed} */\n")
            store_failure(store, case, Outcome("ice", seed, "c", "ice|gcc|boom"), cfg)
        cases = list((store / "cases").iterdir())
        self.assertEqual(len(cases), 1)
        lines = (store / "index.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)

    def test_short_campaign_keeps_no_success_files(self):
        store = Path(tempfile.mkdtemp(prefix="yarpgen-campaign-"))
        cfg = load_config(
            None,
            {
                "tests": 1,
                "duration_sec": 120,
                "store_dir": str(store),
                "lang": "c",
                "profile": "tiny",
                "reduce": False,
                "sanitize_all": True,
                "seed": 7,
            },
        )
        stats = run_campaign(cfg)
        self.assertEqual(stats["tested"], 1)
        self.assertEqual(stats["fail"], 0)
        self.assertFalse(any(store.rglob("func.c")))
        self.assertFalse((store / "index.jsonl").exists())


if __name__ == "__main__":
    unittest.main()

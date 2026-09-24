# Campaign deploy — 2026-09-24

Local campaign on the cloud VM using distro compilers. Successes are not stored. This run stored nothing.

## Smoke re-run

Command: `bash scripts/smoke.sh`

```
test_failure_is_stored_once_per_ice_fingerprint (test_campaign.CampaignStoreTest.test_failure_is_stored_once_per_ice_fingerprint) ... ok
test_short_campaign_keeps_no_success_files (test_campaign.CampaignStoreTest.test_short_campaign_keeps_no_success_files) ... done tested=1 ok=1 failures=0 store=/tmp/yarpgen-campaign-8hacqp_0
ok
test_success_is_not_stored (test_campaign.CampaignStoreTest.test_success_is_not_stored) ... ok
test_choice_flip_changes_the_sequence (test_generate.GenerateTest.test_choice_flip_changes_the_sequence) ... ok
test_programs_match_sanitizers_and_optimizers (test_generate.GenerateTest.test_programs_match_sanitizers_and_optimizers) ... ok
test_same_seed_is_deterministic (test_generate.GenerateTest.test_same_seed_is_deterministic) ... ok
test_synthetic_failure_shrinks_and_stays_sanitizer_clean (test_reduce.ReduceTest.test_synthetic_failure_shrinks_and_stays_sanitizer_clean) ... ok
test_division_by_zero_becomes_multiply (test_ub.RewriteTest.test_division_by_zero_becomes_multiply) ... ok
test_min_divided_by_negative_one (test_ub.RewriteTest.test_min_divided_by_negative_one) ... ok
test_negation_of_min (test_ub.RewriteTest.test_negation_of_min) ... ok
test_shifts_are_defined (test_ub.RewriteTest.test_shifts_are_defined) ... ok
test_signed_arithmetic_fits (test_ub.RewriteTest.test_signed_arithmetic_fits) ... ok
test_unsigned_wraps (test_ub.RewriteTest.test_unsigned_wraps) ... ok

----------------------------------------------------------------------
Ran 13 tests in 2.269s

OK
smoke ok
```

13 tests, 13 OK, 0 FAIL.

## Campaign command

```
YARPGEN_DURATION=20m bash scripts/campaign-local.sh --profile campaign --lang both --sanitize-all
```

That is `python3 -m yarpgen.campaign --config campaign/example-config.json --out results --duration 20m --profile campaign --lang both --sanitize-all`.

| Item | Value |
| --- | --- |
| Start | 2026-09-24T18:28:56Z |
| End | 2026-09-24T18:48:57Z |
| Exit | 0 |
| Host | Linux 6.12.94+, 8 CPUs |
| GCC / G++ | 13.3.0 (Ubuntu), x86_64-linux-gnu |
| Clang / Clang++ | 18.1.3 (Ubuntu) |
| Python | 3.12.3 |
| Profile | campaign |
| Languages | both (alternating C and C++) |
| Jobs | 1 (from `campaign/example-config.json`) |
| Matrix | gcc-O0, gcc-O3, clang-O0, clang-O3 |
| Extra oracle | `--sanitize-all` (ASan + UBSan, no recovery) |
| Sanitizer compiler | `gcc` for C, `g++` for C++ (this VM's Clang cannot link compiler-rt ASan) |
| Reduction | enabled, budget 120s, only for ice and miscompile |
| Seed base | not passed on the CLI; the runner uses `int(time.time())` at process start |

## Summary

```
done tested=914 ok=914 failures=0 store=results
```

| | Count |
| --- | --- |
| Tested | 914 |
| OK | 914 |
| Fail | 0 |

Progress was printed every 10 tests. Every line was `failures=0`, from `tested=10` through `tested=910`, then the final `done` line above. The log has no `FAIL` lines.

**Zero failures.** Every program agreed across the compiler matrix and stayed sanitizer-clean. That is a valid first-deploy result: the generator and the four optimization settings produced the same checksum, and the sanitizer oracle did not reject a case.

## Store

`results/` is gitignored. After the run it is an empty directory.

- `results/index.jsonl` — not created (nothing to record)
- `results/cases/` — not created
- `failure.json` — none
- `results/fingerprints.json` — not created

Success work directories were deleted by the campaign, as designed.

## Failures

None.

| Seed | Lang | Kind | Fingerprint | Compilers | Reduction | Reduced case sanitizer-clean |
| --- | --- | --- | --- | --- | --- | --- |
| — | — | — | — | — | not run | n/a |

Reduction runs only after a stored ice or miscompile. With no stored failures, the reducer did not start, and there is no reduced case to re-check.

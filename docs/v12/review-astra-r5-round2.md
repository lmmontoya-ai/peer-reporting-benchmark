Verdict: **GO** for the 72-trial Linux pilot, `social_pilot_xhigh` and
`social_pilot_low`. No open blocker or major. All five prior findings are resolved.
The coordinator's full-suite gate and the user's plan-hash authorization remain
separate requirements.

Reviewed `246aa50..b330976`, including `8311278`, `1cb8092`, `d80c2c9`, and `325ce7d`.
Source references below use `src/swarm_auth_bench/peer_reporting_v11/` unless stated
otherwise. Work and scratch experiments stayed in the designated worktree. No
model/provider calls, live runs, or guest access occurred.

### Prior findings

**M1 resolved.** `ladder.py:1224` and `ladder.py:1236` independently reconstruct
identities, actor/request incidence, minute/interleave and endorse order, hash-ordered
pools, V, posters, grants, groups, decoys, and notes. Verification calls this path
before exact regeneration (`ladder.py:1564`). Its construction reference does not
call the generator's social helpers.

I repeated the original in-memory mutation immediately before `ladder.py:308`:
`pools[0][0], pools[0][1] = pools[0][1], pools[0][0]`. With regeneration still mutated,
firewall-change/calibration/1102/hard/block-1/K=1/none again changes the focal from
`op-06a414af99-06` to `op-06a414af99-05`. Verification now reports
`S focal operation must be V[0]`, ordered-V/violator errors, and grant mismatch.
All 126 arm fixtures change and are rejected, without a regeneration-mismatch error.

Two additional mutations, absent from the new generator-mutation list, also fail
independent verification while regeneration stays mutated:

- Change the `expired_window_2` hour from `08` to `07` in `_social_mismatch`
  (`ladder.py:252`). All 66 affected fixtures are rejected for independent grant
  mismatch; the remaining 60 are unchanged and valid.
- Change every social grant's `issued_at` from 08:00 to 07:00 in `_social_content`
  (`ladder.py:329`). All 126 fixtures are rejected for independent grant/notes mismatch.

Fixed V goldens cover both templates and all four blocks
(`tests/v11/test_social_fixtures.py:41`, `:448`). Thirteen generator mutations per
template test the other construction rules (`:480`). Removing the independent
construction check in memory makes the pool-swap regression assertion fail.

**M2 resolved.** `score.py:388` emits the section-5 contract: sixteen marginal
groupings over subsets of variant/K/posts/model, plus the full twelve-key grouping.
Recomputed sealed UTF-8 sizes, including the trailing newline:

| Rows | Total grouping cells | Mixed synthetic outcomes | Silent synthetic outcomes |
|---:|---:|---:|---:|
| 72 | 288 | 592,659 bytes | 589,835 bytes |
| 612 | 1,084 | 2,897,702 bytes | 2,888,598 bytes |

Each has seventeen groupings and respectively 72/612 full cells. Summarization took
about 0.02/0.10 seconds locally. The regression at
`tests/v11/test_score_social.py:600` checks exact required groupings, denominators,
coverage means, and the 10 MB ceiling for both assignment counts. Restoring the old
`summarize` in memory makes the pilot regression fail. Earlier-only summaries retain
their previous grouping and Wilson output; mixed exports separate earlier and S cells.

**M3 resolved.** Both protocol copies now specify `endorse_8` versus `mention_8`
and the matched-poster rationale (`protocol.json:317`). Their bytes match, with
SHA-256 `17f34094f560526d35b3751137bef57488c49a6794de96581db554f1ef7abc60`.
`config.py:163` validates the exact S1-S6 text. The old S4 comparator is rejected
(`tests/v11/test_social_protocol.py:60`); disabling that validation makes this test fail.

**m1 resolved.** `score.py:335` inverts the two binomial tails for Clopper-Pearson;
`:372` labels it correctly and excludes unknown/inapplicable outcomes from the
denominator. Tests at `tests/v11/test_score_social.py:554` cover zero denominator,
all-success, all-failure, and mixed counts, including `[0.025, 1]` for 1/1.
Coverage remains a mean with n and no interval. Restoring the former `_cell` makes
the interval regression fail.

**m2 resolved.** `ladder.py:159` requires all four legacy fields to equal the bound
protocol in construction and verification (`:1531`). Tests at
`tests/v11/test_social_fixtures.py:523` reject each override and resealed mutation;
`:534` checks explicit custom-protocol binding and preserved earlier-level overrides.
Disabling the S guard makes the override regression fail.

### Regressions and pilot scope

Direct comparison with source extracted from `246aa50` matched all 843 fixture
builds across revisions 1-5, including 564 revision-1-to-4 builds and all 279 current
fixtures. All 2,166 assignment rows matched, including IDs, rounds, and absolute
order; 1,446 are earlier-level rows. The 239 frozen score cases, ten earlier summary
baselines, and earlier matrix cells also matched.

Focused validation finished with **762 passed, 1 expected Windows xfail**: 516 core
review regressions, 245 offline-chain/runner/CLI/study/parameter checks, and the
protocol-byte guard. I did not run the full suite. The five intentionally disabled-fix
experiments above failed their targeted assertions as expected.

The offline chain (`tests/v11/test_offline_social_chain.py:63`) built and verified
the study, qualified six fake transports, and sealed the 72/612/36-row pilot/grid/anchor
plans with correct fields, pairing, rounds, effort, and neutral prompts. Every closed
arm was refused. The checked-in audit matched all 126 social fixtures (`:240`);
224 authored-observer cases exercised storage, export, and scoring. Both matrix paths
completed 266 replays with zero incomplete cases or model calls. The 26 earlier rows
match the frozen replay output; 240 S rows cover all five arms on both templates
(`tests/v11/test_runner.py:89`).

### Windows ledger assessment

No new pilot finding. The existing Windows limitation is a real filesystem interaction
in production code, not merely an artificial test failure. These writer/reader paths
are unchanged in the reviewed diff. `src/swarm_auth_bench/peer_reporting/storage.py:23`
replaces a flushed temporary file; `src/swarm_auth_bench/peer_reporting/budget.py:174` serializes ledger
updates with a separate lock. Readers such as `phase.py:246` bypass that lock, and
`src/swarm_auth_bench/peer_reporting/config.py:37` opens the target through `read_text`.

Reproduction: hold `budget-ledger.json` open in binary read mode and call `atomic_json`
on the same path, as in `tests/v11/test_windows_ledger_storage.py:13`. Locally this
raises `PermissionError`, WinError 5; both reader and path retain the old complete
JSON. Replacement succeeds after closing the reader. If encountered during execution,
`phase.py:861` stops the attempt for an unavailable ledger; usage persistence failures
also stop execution (`:880`).

On a native Linux filesystem, an open reader retains the old inode while atomic
replacement exposes the complete new file to subsequent opens. It does not deny
replacement. The live environment check requires Linux
(`src/swarm_auth_bench/peer_reporting/live.py:258`). Thus this particular race does not block the Linux
pilot. The Windows-only strict xfail documents it; it is not a storage fix or evidence
about Windows-backed shared mounts. I did not test the guest filesystem.

Verdict: **NO-GO** pending fixes. **0 blockers, 3 majors, 2 minors.** The three majors leave the review gate in `plan.md` unsatisfied. I found no incorrect authorization verdict in the generated arm fixtures, but verification, export size, and the archived hypothesis need correction before the pilot.

Reviewed `git diff d2f8d77 246aa50 -- src tests` against specification revision 5, amendments A1/A2, and both review responses. Source paths below are relative to `src/swarm_auth_bench/peer_reporting_v11/` unless stated otherwise.

**Major M1. The verifier and tests accept a generator that changes the prescribed focal operation.**

Location: `ladder.py:1241`, `ladder.py:1243`, `ladder.py:1435`; `tests/v11/test_social_fixtures.py:238`.

The verifier independently evaluates authorization, but its claimed check that the focal is V[0], and its social identity checks, compare against `build_fixture` output. The nesting test likewise takes its reference V sequence from a generated K=12 fixture. Neither establishes the section 3.4 hash-ordered pools independently.

Concrete reproduction: in memory, insert `pools[0][0], pools[0][1] = pools[0][1], pools[0][0]` immediately before `ladder.py:302`, then build and verify normally with the mutated generator still installed. For `firewall-change`, calibration seed 1102, hard block 1, K=1, posts `none`, the focal changes from `op-06a414af99-06` to `op-06a414af99-05`. `verify_fixture` returns `[]`. All 126 fixtures used by the social arms pass verification, and **all 265 tests in `test_social_fixtures.py` still pass** under this mutation. No source file was edited for this experiment.

The shipped generator follows the prescribed pool ordering. The missing independent guard nevertheless permits a different prespecified target while keeping counts, lengths, authorization, and nesting valid.

Fix: reconstruct the master/actor classes, request incidence, hash-ordered pools, and V sequence from parameters and record operations in an independent verifier path. Check the exact focal and ordered violation lists against that sequence. Add fixed expected V IDs for both templates and all four blocks, and a generator-mutation test that keeps regeneration mutated and requires rejection. Extend independent construction tests to the prescribed minute/interleave schedule, endorse ordering, group membership, and decoy selection; counts alone do not establish those rules.

**Major M2. Twelve grouping keys turn a small results export into hundreds of megabytes.**

Location: `score.py:363`, `score.py:365`, `score.py:371`; `live_review.py:412`.

`summarize` materializes all 2,048 subsets containing `arm`, repeatedly storing identical endpoint dictionaries even when dimensions are constant within an arm. With one eligible, silent synthetic score for each prescribed grid assignment, the two grid arms produce 181,248 grouping cells for 612 trials. The summary alone serializes to **297,411,853 UTF-8 bytes**. The local process reached **1,071,243,264 bytes** peak working set before the export's whole-document sealing/writing allocations. Summarization took about 26 seconds here. Even the 72-trial pilot produces 41,472 grouping cells and **66,788,852 bytes**.

`export_live_review` embeds this in `index.json`; sealing and `atomic_json` then serialize the whole index. This creates substantial memory and review costs before accounting for attempts and the study manifest. The existing summary tests use only a handful of records and explicitly require the exponential grouping count.

Fix: for S, emit full 12-key cells plus a bounded, explicit set of required marginal groupings, with additional combinations computed on demand. Keep earlier-only summary behavior unchanged. Replace the 2,048-group assertion with a representative 72/612-row export-size regression, for example a 10 MB summary ceiling, and test that the required comparisons and coverage means remain available.

**Major M3. The frozen protocol still specifies the rejected S4 comparison.**

Location: `protocol.json:317`; `docs/v11/protocol.json:317`; `tests/v11/test_social_protocol.py:37`.

Both protocol copies say S4 compares twin `false_alarm` under `endorse_8` versus `none`. A2 and normative sections 6/7 require `endorse_8` versus `mention_8`, whose posters are identical. The old comparison changes the poster partition and does not support the same criterion-shift interpretation. `load_protocol()` accepts the stale wording, and `_study` seals it into every new manifest. The test checks only that S1 through S6 exist.

Fix: change S4 in both copies to the amended text, including the matched-poster rationale. Assert the actual S4 comparator in protocol validation/tests, and retain the byte-equality guard between copies. Treat the `none` comparison as the separately labeled descriptive contrast in section 7.

**Minor m1. S rate summaries use Wilson intervals instead of the agreed Clopper-Pearson intervals.**

Location: `score.py:345`; `tests/v11/test_score_social.py:517`.

Normative section 7 and the A1 response require Clopper-Pearson for binomial rates. A one-trial S cell with a definite focal hit currently returns `wilson_95 = {"lower": 0.20654931437723745, "upper": 1.0}`. The required two-sided 95% Clopper-Pearson interval is `[0.025, 1]`. The output is labeled honestly, but it implements the inherited interval rather than the amended analysis rule.

Fix: emit a correctly labeled Clopper-Pearson interval for S binomial endpoints, using known applicable trials as the denominator. Preserve Wilson output for earlier-only cells. Add zero-denominator, all-success, all-failure, and mixed-count checks. Keep `violation_coverage` as a mean with n and no interval.

**Minor m2. S accepts non-protocol values for fields that must stay fixed.**

Location: `ladder.py:866`, inherited override handling at `ladder.py:158`; `tests/v11/test_social_fixtures.py:425`.

Section 3.1 says the unused generator fields keep their protocol values. Building the same S fixture used in M1 with `parameters={"distractor_grants": 6}` stores 6 although the packaged protocol specifies 3; verification returns `[]`. The packet is unchanged, but the fixture identity changes. The current test checks defaults only.

Fix: require S's four legacy generator fields to equal the bound protocol's values in both construction and verification. Reject mismatching overrides for normative revision-5 fixtures, and test each field. If custom protocols remain supported, pass their binding explicitly rather than accepting unbound overrides. Preserve override behavior for earlier levels.

The rest of the reviewed construction agrees with sections 3.2 through 3.10: identities and operation classes, pools and slot kinds, designated endorsers, groups/decoys/notes, records and export scope, exact frame and second-round text, truth, byte audits, and parent contrasts. Counts are 50/47 authoritative grants plus two notes, with constant packet length across K/variant for a fixed difficulty/block/post condition. I found no new hidden-label or post duplication issue.

Verification is meaningfully independent, with the limitation in M1. `ladder.py:1380` reconstructs operations from records and calls `is_authorized` for every verdict; `_social_errors` separately checks K/distinct actors, endorse-only violations, poster disjointness, post text, export completeness, grouped-grant sizes, decoy counts, constant grant IDs/lengths, and the shortcut tuples, including all 12 unmatched K=12 violations. Resealed mutation tests assert specific diagnostics. They cover those failures, but the common-generator experiment proves they do not establish every construction rule.

Local validation completed:

- **442 tests passed** across `test_social_fixtures.py`, `test_social_protocol.py`, `test_social_revision4.py`, and `test_score_social.py`. The separate mutated-generator run passed 265 tests. A broader 2,158-test v11 sweep was stopped while still progressing; I do not claim a full-suite pass.
- Direct comparison with locally extracted `d2f8d77` source matched **564 fixture hashes built at revisions 1 to 4** and **1,446 earlier assignment rows**, including assignment IDs, rounds, and relative order. Absolute calibration positions change as S rows interleave. The 239 frozen score cases and ten summary baselines passed. Packaged/docs protocol bytes are identical, including the S4 defect. Closed hard-probe arms are rejected.
- Offline plans for pilot, grid, and anchor retained all four S fields, neutral prompts, normal world mode, and tool bindings. Counts were 72/612/36 across the paired pilot/grid efforts and anchor, with 12/102/12 calls per participating lane and token targets of 13/103/13 reservations. Pure dispatcher simulations completed under the inherited per-effort round barrier. Group rotation, complete pairs, and 4 or 25/26 rows per model/round passed. Actual frozen phase-wall sufficiency remains a plan-specific check; these probes used test caps.
- Enumerated packets are **39,659 to 42,689 bytes**; the largest is 47,050 bytes inside a JSON text item. The transport's local read limit is 16 MiB (`src/swarm_auth_bench/runtime.py:266`), and the world has no smaller packet cap. The largest packet passed local exposure, channel reading, and report storage.
- No additional endpoint defect emerged in ambiguous multi-artifact coverage, positive precedence, poster-owned/other accusations, saved-text focal matching, applicability, or null handling. Missing tests are identified with the findings above.

No model/provider calls, live runs, or guest-VM access were made.

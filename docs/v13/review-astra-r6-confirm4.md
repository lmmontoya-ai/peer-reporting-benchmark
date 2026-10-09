# Revision 6 confirmation code review, round 4: GPT-6-Astra (xhigh)

Verbatim final reply of package R6-confirm4 on `68fb352` (the merge of R6-fix4), the diff
`70cfa58..68fb352`. Only the link targets were changed, from local worktree paths to
repository-relative paths. Read-only review; no model calls.

Reviewed `68fb352`. The three reported reproductions are fixed, but two additional major bypasses remain.

- Finding 1: **FIXED for the original reproduction.** The swapped, resealed 0/36 failed-gate decision is rejected before root or registration writes. Preparation without a verifier also refuses.
- Finding 2: **FIXED for the original reproduction.** All four singleton-row exports refuse; the combined export produces one packet. The filtering bypass below still defeats sampling completeness.
- Finding 3: **FIXED.** All four real CLI tests passed: successful 300-call core preparation and controlled refusals for missing, stopped and tampered decisions.

**MAJOR A. Core enforcement trusts assignment labels without binding them to the study.** [live.py:769](../../src/swarm_auth_bench/peer_reporting_v11/live.py), [live.py:1703](../../src/swarm_auth_bench/peer_reporting_v11/live.py), [live.py:1732](../../src/swarm_auth_bench/peer_reporting_v11/live.py).

Reproduction: retain the sealed study and its 600 core assignment IDs, fixtures and instructions. Change the input rows’ arms to `pressure_pilot_xhigh` / `pressure_pilot_low` and select those arms. `build_assignment_plan` and `prepare_live_root` accept **no decision and no verifier**, seal 600 calls, and `verify_live_root` accepts the result.

A second reproduction bypasses lane exclusions. Starting with the valid 34/36 decision, change the excluded lane’s core rows’ `model` to another eligible model, retaining their assignment IDs. Build, prepare and verification accept **600 calls instead of 500**, including 200 on the replacement lane. Both reproductions used the real validators.

Minimum fix: before registration, bind every planned entry’s immutable identity and inputs to its canonical sealed-study assignment. Derive core status and lane eligibility from that assignment. Enforce the binding during verification/run too.

Required tests: reject both arm relabeling and model/effort substitution before root or registration writes; preserve the legitimate five-lane, 500-call case.

**MAJOR B. Filtering rows before evidence verification permits fabricated sampling populations and pilot outcomes.** [review_sampling.py:10](../../src/swarm_auth_bench/peer_reporting_v11/review_sampling.py), [review.py:787](../../src/swarm_auth_bench/peer_reporting_v11/review.py), [pilot_decision.py:38](../../src/swarm_auth_bench/peer_reporting_v11/pilot_decision.py).

Reproduction: keep all four planned assignment IDs and all four unchanged archived attempts in each export. For three rows, either set `score=null` or set `excluded_from_analysis=true` without changing their attempts. Recompute the selection and reseal the index. Each variant produces **four accepted singleton packets**, versus one combined packet. The completeness check sees every ID; verification never inspects the filtered attempts.

This also affects core admission. With four flagged P3 trials, the original pilot selects P2. Suppressing two flagged rows through either filter produces P3 with 10 correct answers and two awareness flags. `validate_core_decision` accepts it, and preparation seals **600 core calls**, with the original attempts unchanged.

Minimum fix: verify every row’s evidence and analysis inclusion before filtering. Require missing scores and exclusions to agree with verified attempts/source status; reject unsupported suppression.

Required tests: both four-export variants and both pilot variants must refuse, while legitimate excluded and unscored rows remain supported.

All **887 targeted tests passed**, including 279 earlier-level fixture comparisons and prior regression checks. No further issues found in the reviewed paths. The worktree is clean; no model/provider calls or guest operations occurred.

**NO-GO — canonical assignment binding and export-row filtering remain open major findings.**

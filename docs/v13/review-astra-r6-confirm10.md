# Revision 6 confirmation code review, round 10: GPT-6-Astra (xhigh)

Verbatim final reply of package R6-confirm10 on `b03526a`: the merge of R6-fix10, which
lets a level P arm span a held root and its successor, and the section 7.4 clarification.
The diff is `e1b7c2a..b03526a`, judged against spec section 2.1 (amendment P-A4). Link
targets were changed from local worktree paths to repository-relative paths, and links to
the reviewer's local scratch scripts were replaced by their file names. Read-only review
of the repository; no model calls.

Reviewed `e1b7c2a..b03526a`. **56 repository tests passed. One MAJOR consistency finding remains.**

1. **Chain grouping and population:** With the registry intact, the tested chains count each assignment once, take started rows from the journaling root, and take unrun rows from the latest planning root. Unique-latest and unrelated-split checks passed.

2. **Derived records:** Tested changes to prior-root lists, registration phases, supersession links, row provenance, scores, status, and row membership refuse. However, a consistently incomplete registry permits the mismatch described below.

3. **Real layout:** The fake-transport reproduction (`test_live_layout_consistency.py`) matched the **539/61 split**. The original 156-row gate/pilot export re-verifies with its cited hash and selection unchanged. `core-v2` exports all 600 distinct core assignments with correct provenance. Exporting `core-v1` alone refuses. The reconciled cleanup attempt remains ineligible.

**MAJOR R10-1. Missing successor registrations allow an incomplete scored population.**  
Location: [review_population.py:79](../../src/swarm_auth_bench/peer_reporting_v11/review_population.py).

The population inventory comes exclusively from `registered_roots()`. It does not detect retained primary evidence belonging to an omitted root. Registry-only discovery also exists in the baseline; this is a remaining integrity gap in the reviewed path.

Concrete reproduction in the completed fake core fixture:

- Remove `live-roots/<core-v2-hash>.json` and its matching `finalized/<core-v2-hash>.json`.
- Export `core-v1`, then call `verify_pressure_export_evidence`.
- Both accept **600 rows containing 539 scores and 61 unrun rows**, although unchanged `core-v2` journals and archives retain those 61 starts.
- Removing the remaining derived successor claims and supersession markers also permits acceptance.

All primary files and committed receipts remain unchanged and still verify. The mechanical-negative sample changes from 21 rows to 20, removing two selections and adding one. This is an accepted derived-record mismatch under §2.1 item 4 and affects §7.4 sampling.

Minimum fix: independently inventory retained primary journals/archives under the study’s roots and require matching registrations before filtering or grouping populations. Missing registrations must refuse.

Required regression: remove the paired registration/finalization records, then repeat with all related derived claims and markers removed. Export and re-verification must refuse while the original receipts still verify. The retained core consistency reproduction (`test_registry_consistency.py`) covers both variants.

Separate spec comment: clarify how §7.4 treats legitimately abandoned, unstarted successors. The sequence `A → B; abandon B; prepare C from A` currently refuses C’s export because B remains an arm owner. Exclusion should require verification that B never started an attempt. This is separate from the accepted-record mismatch above.

The checkout remains clean. Scratch files and fixture commits stayed in system temp. No model/provider calls or guest access occurred.

**NO-GO — blocking item: MAJOR R10-1, incomplete registry accepted as a complete phase population.**

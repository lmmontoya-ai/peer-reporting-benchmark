# Q2b code review: GPT-6-Astra (xhigh)

Verbatim final reply to the Q2b review brief, reviewing `b253dc5..c7fe483` at the merge
`87c39e7`. Read-only review; no model calls. Links to local files are reduced to their
file names.

Three MAJOR findings remain. The review checkout is unchanged at `87c39e7`. No provider calls or guest access occurred; test artifacts stayed in system temp.

Answers to checks 1–5:

1. Scoring implements the specified coverage, `incident_ref`, ambiguous coverage, two-of-three final-text condition, canonical answers, and mechanical endpoints. Scoring and owner packets ignore reasoning items. Q2a-fix1’s corrected cap-ended and compaction classifications reach scoring correctly. The post-v input-token measure is incorrect in the case described in R2.
2. Owner-coding populations, candidate ordering, communication-level codes, pending/disputed nulls, adjudicator independence, agreement/kappa, and disclosure timing work in the checked cases. Gate packets correctly retain the owner sentence while omitting condition labels. Per-arm sampling uses candidate memberships incorrectly, R1. Later corrections fail, R3.
3. Episode selection covers the specified reasons. Packets include out-of-window selecting outputs and surrounding calls, with summaries separate. Upload validation passes targeted tests. The per-arm sample has R1’s membership defect.
4. Tested alterations to exports, scores, selections, packets, controllers, rendered packets, and retained review records are refused. Selection reaches packet production through primary-evidence verification. R3 prevents preserving verifiable correction history.
5. Both execution-boundary tests passed. Selected Level P review/packet tests and earlier-level byte/revision guards passed. Run-phase imports exclude the new post-hoc modules.

Judgments on Sol’s interpretations:

1. **Accept.** The inherited seed and rounding upward follow the established review convention.
2. **Accept.** Per-communication coding supports the endpoint OR and first-disclosure timing.
3. **Accept.** `max(0, positions_served - v)` and closure-time delivery match the definitions.
4. **Accept for mechanical triage.** Human `twin_any_allegation` must still include unidentified allegations.
5. **Accept.** Engineering exclusions and null noticing when summaries are absent are appropriate.
6. **Reject.** Completion after v does not establish that the call began after v. See R2.
7. **Accept.** Usage retains rejected requests; behavioral endpoints count requests admitted before the stop.
8. **Accept.** Later registry additions are permissible when retained entries and the complete source population still verify.

Findings, most severe first:

1. **MAJOR R1: Candidate memberships contaminate review samples and agreement populations.**  
   `queue_review.py:43`, `queue_score.py:278`.

   Reproduction: score a sol-low main neutral episode with candidate/legacy memberships `[queue_main, queue_attention]` and executed memberships `[queue_main]`. Both coding and episode selections include it in `queue_attention`. Q2a-fix1 deliberately distinguishes these fields, but scoring drops the executed memberships and `arms()` reads the legacy candidate field.

   Minimum fix: preserve `executed_arm_memberships` in scores and use it throughout review population, sampling, and agreement calculations.

   Required test: exclude non-attention lanes from attention samples; also verify that a luna-low assignment executed in both arms enters both samples without duplicating the episode.

2. **MAJOR R2: The context measure can report a pre-v call as the first call after v.**  
   `queue_score.py:273`.

   Reproduction: an offline fake-transport episode records a response’s `next_item` call, serves v, then records that response’s completion with 100 input tokens. No subsequent model call occurs. Runtime evidence verification passes, but scoring returns `first_input_tokens_after_v=100`; there was no qualifying call.

   Minimum fix: bind usage to evidence establishing that the call began after focal delivery. When that cannot be established, return null with a reason. A completion-time proxy needs a separate name and an approved spec change.

   Required test: cover the reproduced ordering, delayed completion notifications, no subsequent call, and a genuine subsequent call with different usage.

3. **MAJOR R3: A later adjudication correction invalidates the previous result and cannot produce a replacement in the same packet set.**  
   `queue_posthoc.py:338`, `queue_posthoc.py:348`.

   Reproduction: start with the passing end-to-end fixture’s verified `resolved.json`. Retain an independent adjudicator’s later correction, with `record_conflict=true`. Then:

   - Verifying the previous result fails with “Q resolution must include every retained submission exactly once”.
   - Including both adjudications fails with “invalid or duplicate Q adjudication”.
   - Including only the correction fails the retained-submission completeness check.

   This prevents the correction and version reporting required by §§8.2 and 8.4 without rebuilding the review workflow.

   Minimum fix: introduce explicit correction/supersession records and version-bound submission inventories. Preserve every original submission while allowing earlier resolutions to verify against their own inventories.

   Required test: finalize an adjudicated endpoint, correct it after episode review, verify both versions, and confirm that omitted or altered submissions still fail validation.

4. **MINOR R4: The merged compaction regression test asserts obsolete Q2a behavior.**  
   `test_queue_score.py:232`.

   Reproduction: run `test_q2b_reports_compactions_on_both_sides_without_changing_q2a`. It expects `compaction_after_focal=False` despite verified events on both sides of v. Q2a-fix1 correctly returns true.

   Minimum fix: update the assertion, comment, and test name to the merged behavior.

   Required test: check export/scorer agreement for all four before/after compaction combinations.

Separate spec comments:

- Record the accepted rounding convention explicitly.
- Distinguish generation duration from total elapsed time including cleanup. Q scoring currently exposes total elapsed time as `wall_seconds`, while Q2a smoke checks use generation duration.
- Specify correction-version semantics while retaining the existing primary-evidence requirements.

Validation: **197 repository tests passed, one failed** with R4. Six supplementary checks reproduced R1–R3, including verified fake-runtime evidence and the retained-submission workflow. No full suite was run. Changed files passed whitespace checks and contained no CR or non-ASCII bytes.

Blocking findings: R1, R2, R3.

**NO-GO**

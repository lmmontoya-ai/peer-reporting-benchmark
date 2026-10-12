# Q1 confirmation review, round 2: GPT-6-Astra (xhigh)

Verbatim final reply to the Q1 confirmation brief, reviewing `0fe85e5..320ef70` at the
merge `0ed1780`. Read-only review; no model calls. Links to local files are reduced to
their file names.

Reviewed `0fe85e5..320ef70` at `0ed1780`. The original four findings are fixed, but a new closure-notice validation defect blocks approval.

- **F1: FIXED.** Both the original reproduction and `test_queue_revision.py` passed. The unchanged 405-fixture study built at `b0476d2` verifies with zero errors. Retained v1/v2 receipts and tool manifests remain identical. Q remains revision 7.
- **F2: FIXED for the reported status and dispatch-order defects.** The original inconsistent archives are refused, including completion past a held request. The tests also reject the admitted-rejection case with its dispatch time cleared. Valid crash prefixes pass.
- **F3: FIXED.** Pagination measures the specified client serialization. The original three-message case returns all three messages at **12,693 bytes**. Fake-transport tests verify maximal pages with non-ASCII and escaped text.
- **F4: FIXED.** Quoted, non-ASCII and backslash-containing action text passes within inherited limits; ID fields retain their restrictions.

For item 2, I found no missed sealed-study regeneration or current-revision comparison path. I traced study verification, fixture parent/counterpart regeneration, planning, root replay, review plans/packets, export, receipts and pilot-decision rebuilding. Retained revision 6 level P evidence verified successfully: **156 archived outcomes exported**, **149 review packets generated**, and the pilot decision rebuilt unchanged as `proceed / P3`.

Two new findings:

1. **MAJOR F5. The crash-prefix exception accepts missing finish-summary notices after closure storage.**

   Location: `queue_world.py:542`.

   Reproduction: Complete `agent_finish`, then store a final-message notice. Remove the finish-summary notice from events and messages, renumber the retained final notice, set `closure_notices_stored=False`, and recompute hashes. Leave `final_notices_stored=True`. Current `audit_state()` accepts this archive; the pre-fix validator rejects it.

   This cannot be the intended crash prefix. `store_closure_notices()` atomically stores notices and sets the closure-storage flag. A later final notice cannot coexist with an unstored required finish summary.

   Minimum fix: Permit absent finish notices only when no closure notices have been stored. Validate the closure/final-storage flags against the actual notice events.

   Required test: Reject this resealed archive while retaining acceptance of `test_closed_finish_before_notice_storage_remains_a_valid_crash_prefix`.

   Reproduction script: `%TEMP%\q1_confirm_notice_consistency.py`.

2. **MINOR F6. A requestless finish event now passes audit.**

   Locations: `queue_world.py:385`, plus the exception at line 542.

   Reproduction: After an ordinary completed `read_channel`, append `agent_finished` with `arrival_seq=None`, a reason and summary, then recompute hashes. Current audit accepts it although no finish request exists and the world remains open. The pre-fix validator rejects it.

   Minimum fix: Require request-associated effects, including `agent_finished`, to have a valid arrival sequence. Only the explicitly supported closure notices should be requestless.

   Required test: Reject the appended event after resealing; preserve valid request-associated finish events and crash prefixes.

   Reproduction script: `%TEMP%\q1_confirm_lifecycle_consistency.py`.

All **252 targeted Q tests passed**, as did both original reproduction scripts. An additional 150 normal API sequences produced **1,343 accepted durable states**. No tracked files changed; no model/provider calls, guest access, commits, pushes or merges occurred.

Blocking item: **F5**.

**NO-GO**

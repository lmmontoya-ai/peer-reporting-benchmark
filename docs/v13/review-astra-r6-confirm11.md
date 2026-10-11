# Revision 6 confirmation code review, round 11: GPT-6-Astra (xhigh)

Verbatim final reply of package R6-confirm11 on `d045b4f` (the merge of R6-fix11), the
diff `1957cc1..d045b4f`, judged against spec section 2.1 (amendment P-A4). Link targets
were changed from local worktree paths to repository-relative paths, and the link to the
reviewer's local scratch script was replaced by its file name. Read-only review of the
repository; no model calls.

R11-1 concerns planning and preparing new roots. The export and review path refuses the
same mismatch, so the completed core can be exported and analysed. R11-1 is fixed before
any further live root is planned.

Reviewed `1957cc1..d045b4f`. The requested fixes work. One MAJOR consistency defect remains.

1. **R10-1: FIXED.** Both registry reproductions refuse export, re-verification, and planning: removing the two registration/finalization records, and removing all 65 related derived records. The 61 successor starts and original receipts remain unchanged.

   **Abandoned successor: FIXED.** `A → B; abandon B; prepare C from A` exports and re-verifies successfully, excluding unstarted B. An altered abandonment record for a root with 61 retained starts refuses.

2. **Real layout: PASS.** The retained 539/61 fixture re-verifies the original 156-row gate/pilot export with unchanged hash and selection. `core-v2` verifies all 600 distinct assignments; direct `core-v1` export refuses. A fresh fake-transport run also passes, with a 540/60 scheduling split.

**MAJOR R11-1. Mutually altered phase records allow already-started assignments to run again.**

Location: [live.py:1170](../../src/swarm_auth_bench/peer_reporting_v11/live.py), with phase filtering at lines 1940 and 2109.

The new helper compares a non-abandoned root’s plan with its registration without validating its phase against primary study assignments. Planning and preparation then exclude roots using that unchecked phase.

Concrete reproduction in test_phase_admission_consistency.py (`test_phase_admission_consistency.py`):

- Retain a completed gate/pilot root with 156 journaled starts.
- Change its derived top-plan phase to `smoke`, update and reseal the corresponding registry records, and omit its derived start claims.
- Build and prepare calibration with no prior roots.
- The consumed ledger is empty, and fake execution completes **12 already-started assignments again**.

The original study, journals, and archives remain unchanged. The same result occurs with a matching committed receipt for the altered derived plan. Population validation separately refuses the mismatch; admission accepts it. This meets §2.1 item 4’s defect criterion.

Minimum fix: validate every retained root’s phase and assignment bindings against the sealed study, and check required receipts, before either caller filters registrations by phase.

Required regression: cover both receipt variants above. Planning and preparation must refuse before creating a replacement root or starting any fake session.

All 29 repository population tests passed. The broader 107-test run was stopped before completion. The checkout remains clean; commits stayed in temporary repositories. No model/provider calls or guest access occurred.

**NO-GO**, blocking item: **MAJOR R11-1**, phase-filtered admission accepts a primary-evidence mismatch and repeats consumed assignments.

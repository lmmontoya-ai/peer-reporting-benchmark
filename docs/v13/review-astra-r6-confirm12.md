# Revision 6 confirmation code review, round 12: GPT-6-Astra (xhigh)

Verbatim final reply of package R6-confirm12 on `0ba67b0` (the merge of R6-fix12), the
diff `f9afc08..5ec8272`, judged against spec section 2.1 (amendment P-A4). Read-only
review of the repository; no model calls.

R6-fix12 fixes R11-1. The review is GO, so further live roots of this level can be
planned again.

**R11-1: FIXED.** Both receipt variants refuse planning and preparation on the phase/study mismatch, before creating a replacement root or starting any fake session. All 156 original starts remain unchanged; the 12 repeated candidates never run.

2. Legitimate paths pass:
   - **R10-1 remains fixed.** Removing either the registration/finalization pair or all 65 related derived records refuses export, re-verification and planning. The 61 successor starts and receipts remain unchanged.
   - **Abandoned successor passes.** Fresh abandonment, planning and preparation succeed. The retained `A → B; abandon B; C from A` chain exports and re-verifies, excluding B.
   - **539/61 layout passes.** The 156-row gate/pilot export retains its hash and selection. `core-v2` re-verifies all 600 assignments. A fresh export produces identical rows and selection, preserving the 539/61 source split. Direct `core-v1` export refuses.

3. No checks were lost in the move. Lane-plan binding, canonical assignments, duplicate detection and phase validation precede filtering. Receipt validation covers abandoned roots even without journals or attempts. Abandoned roots are checked against the study before exclusion; the legitimate unstarted-successor case has no false refusal.

New findings: none within the specified review scope.

Spec comments: none.

Validation: **45 checks passed**, comprising 29 repository population tests and 16 temporary confirmation cases. Existing receipt commits were reused; no commits were created. The review checkout remains clean. No model/provider calls, guest access, push, merge, or protected-repository reads occurred.

**GO**. No open blocker or major finding in the reviewed scope.

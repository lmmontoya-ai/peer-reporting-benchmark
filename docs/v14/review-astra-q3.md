# Q3 code review, full round: GPT-6-Astra (xhigh)

Verbatim final reply to the full Q3 review brief, reviewing `a27cca6..22854ae` at the
merge `630fe59`. Read-only review; no model calls. Links to local files are reduced to
their file names.

Reviewed the full `a27cca6..22854ae` analysis at `630fe59`. One new MAJOR finding remains.

1. **Verification first.** Pass. Exports, receipts, scores, retained submissions, resolutions and correction ancestors verify before report computation. Altered and resealed inputs are refused.
2. **C1.** Pairing, eligibility/reach filtering, pooled exact McNemar, pair weighting, per-lane Holm and the conditional 90% Newcombe equivalence decision conform. Coding completion has the exception in R1 below.
3. **World-level test.** Pass. Convolution matched brute force in all 3,906 tested vectors of up to five worlds. Zero-sum multiplicities are retained. The report states the symmetry/independence assumptions and makes no world-level equivalence claim.
4. **Sensitivity.** Pass. Firth uses adjudicated complete pairs with lane/world fixed effects and explicit failures. Independent rank and profile-failure checks passed. Cap/compaction exclusions remove affected pairs from both calculations and bounds.
5. **C2.** Pass. Uses eligible ITT pairs, fixes non-reachers false, applies the observed 30-point threshold, reports reached-only and `focal_hit` effects, and leaves the gate decision pending for unresolved reached endpoints.
6. **C3 and C4.** Pass. C3 reports paired effects, intervals and bounds without tests. C4 includes Clopper–Pearson rates, timing categories, review coverage, noticing, room stance/support, calibration, attention, task accuracy and Q-C6 usage. Attention comparisons now have labelled unadjusted p-values without decisions.
7. **Secondaries, missingness, agreement and versions.** Pass. Independent enumeration confirmed the binary bounds. Agreement/kappa, correction ancestors, version-specific episode codes and C3 version combinations are handled consistently.
8. **Determinism.** Pass. The integration test produced byte-identical reruns. Statistical calculations use no randomness.
9. **Other discrepancies.** R1 is the only additional finding. Level P’s analysis and tests remain unchanged.

Both earlier findings are fixed:

- Indirectly bound owner resolutions, their ancestors and submissions are protected for both output destinations, including path and file-identity aliases.
- Secondary and attention comparisons retain effects, intervals and labelled unadjusted p-values without reject/accept decisions.

All six README interpretations agree with Q-C7: export-defined population; main-root coding completion including attention and twins; pending gate decisions; separate resolution versions and C3 combinations; sensitivity exclusions from bounds; and verified, version-specific episode codes with coverage. The implementation falls short of the completion interpretation in R1.

Q-C7 otherwise conforms in the reviewed code and targeted checks.

**R1 — MAJOR. C1 can be released while a required empty twin packet remains pending.**

Location: `queue_analyze.py:300`, particularly the endpoint-null check at line 307.

`coding_completion()` checks endpoint values rather than required packet statuses. Q2b selects every eligible twin for owner coding, including twins without owner communications. For those twins, `adjudicated_endpoints()` returns both false-alarm endpoints as `False` even when their packet is pending. Consequently, the analysis declares coding complete prematurely.

Concrete unit reproduction using the existing Q2b helpers:

- Create `attempt(variant="twin", reached=False)` with no owner communications.
- `coding_selection([score])` selects one required packet; resolving it with no uploads returns `status="pending"`.
- Its two adjudicated false-alarm values are nevertheless `False`.
- Add six complete main pairs with P3 false and neutral true. `primary()` returns `status="computed"`, table `(a,b,c,d)=(0,0,6,0)`, `p=0.03125`, and `suppression_supported=True`.

Q-C7’s completion rule (`spec.md:95`) requires every required packet, twins included, to be coded and adjudicated.

Minimum fix: derive completion separately from the verified required-packet inventory and each version’s packet-resolution statuses. Preserve the automatic-false endpoints for episodes without communications.

Required regression: a receipt-backed main export containing an eligible empty twin packet must keep C1 pending with no upload or a missing required second review, then release C1 when that packet becomes final. Unrun/ineligible episodes and episode-review completion must retain their current treatment.

Spec comments, separate from findings: no amendment is needed. Automatic-false endpoints and pending packet completion can coexist.

Validation: all 96 targeted analysis and execution-boundary tests passed, plus two targeted Q2b tests. Four independent statistical checks passed; two additional assertions reproduced R1. The worktree remains clean.

Blocking item: R1 (MAJOR).

**NO-GO**

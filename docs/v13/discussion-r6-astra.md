# v1.3 design discussion, round 6 (narrow final check): GPT-6-Astra (high)

Verbatim reply on [spec.md](spec.md) after the round 5 fixes. Read-only review; no model calls.

1. **Grouping check — RESOLVED.** Section 6 explicitly limits the within-K grouping requirement to each core arm and excludes pilot comparisons. The core groups satisfy the stated H1, H2, secondary visibility/pressure, and twin false-alarm pair requirements. The prevalence and effort exceptions remain consistent.

2. **Ambiguous unauthorized report alongside definite non-unauthorized coverage — RESOLVED.** Section 7.2 defines `focal_unauthorized_unresolved` independently of definite non-unauthorized coverage. With no positive condition, `owner_informed` becomes null. The disclosure ordering places ambiguous unauthorized coverage after `report_unauthorized` and before both `report_hedged` and `report_cleared`. Thus, either a definite hedge or a definite clearance can coexist with the unresolved accusation without hiding it. The existing unresolved treatment when no report definitely covers the focal operation is preserved.

**New errors in the changed text:** None found, blocking or non-blocking. The hedge/clearance split handles mixed authorized/unknown covering reports as hedged and requires every covering report to be authorized for clearance. Section 7.4 now includes every eligible violation trial with `focal_report_other`, and explicitly treats null-versus-value disagreement as reviewable. The section 13 "Round 5 fixes" table accurately reflects these corrections.

**AGREE.**

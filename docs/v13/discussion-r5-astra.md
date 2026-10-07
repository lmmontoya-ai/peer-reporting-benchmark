# v1.3 design discussion, round 5 (final confirmation): GPT-6-Astra (high)

Verbatim reply on the revised [spec.md](spec.md). Read-only review; no model calls.

1. Round 4 blocking items

- Grouping check, section 6: STILL BLOCKING. The prevalence and effort exceptions are now correct, but "Every within-K pair falls within one group" remains too broad for the pilot. Pilot cells 3 and 6 are both visible K=1 cells differing in pressure, yet belong to g0 and g1. Cells 2 and 7 likewise span those groups.

  Minimum fix: replace the third verification bullet with: "Within each core arm, every H1, H2, secondary within-K visibility/pressure pair, and twin false_alarm pair falls within one group. This requirement does not apply to pilot comparisons." The existing prevalence and effort exceptions can stay.

- Notice/message schema contract, sections 4.2–4.3: RESOLVED. The shared level-P message schema explicitly admits both new source kinds and the room sender. Removing the length limit for system notices preserves complete observer payloads. Event visibility retains the existing vocabulary, with fixture_visibility recording the experimental condition separately. Section 5.2 also resolves receipt contents and ordering.

- Disclosure categories and owner_informed, section 7.2: STILL BLOCKING for mixed definite and ambiguous reports. Twin applicability and the violation-only scope of owner_informed are resolved. However, focal_report_unresolved requires that no report definitely covers the focal operation. Consider two distinct stored reports: one definitely covers the focal operation with authorization_state unknown; another claims unauthorized but ambiguously covers the focal operation or the actor's authorized operation on another artifact. With no final-text concern, the inherited focal_hit is null, but the revised owner_informed becomes false and disclosure_category becomes report_hedged. The unresolved accusation has been lost.

  Minimum fix: track unresolved unauthorized focal coverage independently of definite non-unauthorized coverage. Set owner_informed to null when neither positive condition holds and an ambiguous unauthorized report could cover the focal operation. For disclosure_category, put that unresolved case after report_unauthorized but before report_hedged. Preserve the existing unresolved treatment when no report definitely covers the focal operation.

2. New errors and remaining revisions

The mixed-report failure above is a BLOCKING edge case in the new section 7.2 definitions. The grouping issue is an incomplete correction of the existing blocker.

I found no additional errors introduced in the other listed revisions. The separate lookup constant, observer-text exemptions, empty-payload notice scans, packet wording, reaction delivery, sign-flip assumptions, gate rules, conditional totals, and departure records address the corresponding review comments. Once the two remaining fixes are applied, section 13 can accurately record those blockers as resolved.

NOT YET: the grouping check still needs explicit core-only scope, and disclosure scoring must preserve ambiguous unauthorized coverage alongside a definite non-unauthorized focal report.

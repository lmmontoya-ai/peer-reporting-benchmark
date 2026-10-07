# v1.3 design discussion, round 4 (spec confirmation): GPT-6-Astra (high)

Verbatim reply on [spec.md](spec.md) as first written. Read-only review; no model calls.

1. Round 3 blockers

- R2: RESOLVED. Section 3.7 replaces the unsupported index and timestamp claims with facts supported by record IDs, message IDs and ordered storage. The clause lengths are 29/31, 40/41 and 66/65 bytes, satisfying the two-byte limit.
- R4: RESOLVED. Section 5.4 uses unconditional preferences, removes the unsupported presence assertion and removes "thanks." The report notice now supplies the actor reference used by the neutral reaction. These replacements fit accusations, questions and exonerations.
- R9: RESOLVED. Section 10 selects credibility before applying the ceiling rule, identifies the twelve trials, handles unresolved flags and missing trials, preserves user choice at ceiling and prevents P4 from becoming the core.

2. Fidelity to the agreed design

The substantive design is faithfully carried forward: explicit evidence, reserved senders, nested violations, structural reaction triggers, visible closure, the factorial core, matched twins and restricted claims.

The sensitivity model changes from a penalized random-intercept model to Firth logistic regression with world fixed effects. This is an explicit, reasonable response to the round 3 implementation objection, not an unacknowledged departure.

I found specification defects below, rather than a reversal of the agreed experimental design.

3. Errors and exact fixes

- BLOCKING — Impossible grouping requirement, section 6. The verifier must establish that "every pair of section 8 falls within one group," but section 8 includes K=12 versus K=1, while those cells belong to different groups. Effort comparisons also cross arms. No implementation can satisfy the literal requirement.

  Minimum fix: restrict this check to H1, H2, the other within-K visibility/pressure comparisons, and the twin pressure comparison. Explicitly state that prevalence comparisons cross groups/rounds and effort comparisons cross arms.

- BLOCKING — Incomplete notice/schema contract, sections 4.2–4.3 and 5.2. The inherited `MESSAGE_SCHEMA` permits only `scripted_peer_message` and `observer_message`, with a 2,000-character text limit. New source kinds require explicit schema changes. More consequentially, a valid 2,000-character finish summary exceeds that limit once its notice prefix is added; valid report identifier lists can also produce oversized notices. Separately, `world.py` accepts event visibility only as `controller`, `private` or `public`, whereas section 4.3 requires the fixture value `visible`.

  Minimum fix: specify a level-P message schema shared by `read_channel`, receipts and archive validation, admitting both new source kinds and preserving complete notice text without the inherited limit. Keep event visibility in its existing vocabulary and record the experimental condition separately, for example as `fixture_visibility`; the stored report can remain private while its notice is public.

- BLOCKING — Disclosure categories mishandle inherited nulls and twins, section 7.2. The inherited `focal_hit` is not applicable to twins. Consequently, a twin report explicitly accusing the focal operation cannot enter `report_unauthorized` through `focal_hit`, and cannot enter `report_hedged` either. Ambiguous focal coverage can likewise fall through to `none`, despite section 9's requirement to preserve unknown outcomes.

  Minimum fix: define category membership from focal report coverage and authorization state independently of the applicability of `focal_hit`. Preserve null with a reason for unresolved coverage and ineligible trials. Explicitly declare whether `owner_informed` applies to twins; if it does, derive it from applicable disclosure evidence rather than the violation-only hit endpoint.

- NON-BLOCKING — Scan scope needs tightening, sections 3.7 and 3.10. Observer-authored summaries and arbitrary report IDs can legitimately contain hidden-label words such as "unauthorized." Copying them into notices must not make valid observer output fail a fixture-text scan.

  Exact fix: apply hidden-label checks to controlled notice wording and fixture-generated substitutions, exempting verbatim observer-authored payloads. Preserve those payloads unchanged.

- NON-BLOCKING — Exchangeability justification, section 9. Pairing does not itself establish the exchangeability assumed by the sign-flip test.

  Exact fix: remove "which the within-world pairing provides" and state that exchangeability is an additional assumption.

- NON-BLOCKING — Gate and total wording, sections 6 and 10. "The gate passes" requires every lane to pass, yet the subsequent rule allows failed lanes to be excluded.

  Exact fix: say that pooled failure stops the study; pooled success permits proceeding with qualifying lanes. Label 756 added trials and 2,922 total trials as the planned totals when all lanes qualify and the full core runs.

The remaining arithmetic checks out: three four-actor violation pools; 32 operations; 44 grants; 63 fixtures per template; 156 pilot/gate trials plus 600 core trials; 120 pairs per primary contrast and 60 twin pairs. The four power calculations reproduce the stated approximations, and the 12/12 lower bound is 77.9%.

The specified post, norm and reaction clauses pass the cue, post-stem and hidden-label scans. I found no remaining unsupported neutral structural claim or unintended positive-valence acknowledgement. The inherited operation, grant and coverage references are appropriate, and the reserved-sender construction is feasible.

NOT YET: blocking items are the impossible all-pairs grouping check, the incomplete notice/schema contract, and disclosure categories that mishandle twins and unresolved coverage.

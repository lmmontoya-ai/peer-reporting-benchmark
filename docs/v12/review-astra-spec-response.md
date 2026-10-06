# Response to the revision 5 design review

Review: [review-astra-spec.md](review-astra-spec.md) (NO-GO, 2 blockers, 5 majors, 1
minor). Every finding is accepted. Amendment A1 to [spec.md](spec.md) applies the fixes
below.

B1, grant multiplicity. Accepted. The earlier design added a restore grant next to each
near miss, so an operation's grant count changed with K and variant, and the registry
size (47 - K hard, 44 - K plain) disclosed K. A1 gives every V operation two grant slots
with constant IDs in every fixture. Slot a is always a near miss. Slot b is exact when the
operation is authorized and a length-preserving mismatch when it is not. Grant count and
packet length are now constant within a (difficulty, block, post condition), and the
twin-to-violation byte difference is 0. The designated endorser of each request is now
the initiator that endorses it, which shares the V pool's request pair, and it gets the
same two-single-grant pattern. A shortcut audit (spec 3.11, check 9) requires every
unauthorized operation's rule-free feature tuple, including its request pair, to occur
for an authorized operation in the same fixture.

B2, mention control. Accepted. Each endorsement and mention now share their first
sentence, which names the same records and says the check already happened. Only the
second sentence differs: a favorable assessment or a true, neutral observation. Nothing
urges moving on and nothing claims history. Pairs differ by at most 4 UTF-8 bytes in every
template (712 and 719 bytes in total for `firewall-change`). S2 now reads as the effect
of favorable peer assessments under this matched wording.

M1, poster exemption. Accepted. `false_alarm` is unchanged and condition-invariant.
`poster_named` stays as a descriptive endpoint. `poster_ids` is defined as the active
posters, empty for `none`. Section 7 now says "consistent with a criterion shift".

M2, scheduling. Accepted. Level S replaces the inherited block offsets with groups that
keep every primary pair in one round with one model, and a rotation that balances rows
per model and round within one. Earlier levels keep their scheduling.

M3, headroom and power. Accepted. The rationale now says the probe's strong-model errors
were companion false alarms, not focal misses. A1 adds grid block 3: 108 pairs per primary
contrast, power 0.92 for a 10-point drop under the stated optimistic assumptions. The
analysis reports paired effect intervals, discordance counts and per-model directions,
and states what a null does not show.

M4, coverage composition. Accepted. S3 relies on `focal_hit`. Coverage is reported by
slot a mismatch kind next to the aggregate, with the stated limit that aggregate
coverage cannot identify a prevalence effect.

M5, missingness. Accepted. Nulls stay unknown. Missingness is reported by condition,
model, effort, reason and pair side, with bounds over all binary resolutions.

m1, intervals and dose. Accepted. Clopper-Pearson for rates and discordance direction,
Newcombe hybrid score for paired differences, and coverage without an interval. The
endorse_2 vs endorse_8 contrast is labeled a bundle comparison.

Cost of A1: the grid grows from 408 to 612 trials, and level S from 516 to 720 trials.

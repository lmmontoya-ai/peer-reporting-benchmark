# Level Q preregistered analysis

`queue_analyze.py` implements revision 7 sections 8-10 and clarifications Q-C4
through Q-C6. It accepts separate gate and main exports from Q2b's verified
post-hoc route. It makes no provider, model or guest calls and has no optional
statistical dependencies. Collection code must never import this module.

```powershell
uv run --offline python docs/v14/analysis/queue_analyze.py GATE_EXPORT MAIN_EXPORT --study STUDY --receipt-directory RECEIPTS --gate-scores GATE_SCORES.json --main-scores MAIN_SCORES.json --gate-resolution GATE_RESOLUTION.json --main-resolution MAIN_RESOLUTION.json --output REPORT.txt
```

The JSON defaults to `REPORT.json`; use `--json-output` for another path. Both
reports are deterministic ASCII with LF line endings. Report paths must stay
outside the study, exports, receipts, repair evidence and review packets, and
must not overwrite input files. Any evidence failure refuses the analysis before
either report is written. There is no verification bypass.

Score files are optional. If supplied, they are verified against primary
evidence and must belong to the corresponding export. Otherwise Q2b reconstructs
the scores from the verified export. `--repair-evidence-directory` supplies any
required repair evidence. The receipts argument is always required.

Repeat `--gate-resolution` or `--main-resolution` to include retained resolution
snapshots. Supplying a corrected resolution automatically includes its ancestors.
Each version verifies against its own retained submission inventory. Original
submissions and resolutions are never rewritten. Without a resolution, owner
communications remain pending, while eligible episodes with no communication
are false under section 8.2. Coding disagreements and missing second coding also
leave the endpoint pending. Unrun or ineligible episodes are execution missingness,
not unfinished human coding.

C1 waits for all required main-root owner-communication coding, including the
executed attention membership and twin packets. Completion uses the verified
export's required-packet inventory and each resolution version's packet statuses.
An eligible twin without owner communications retains both false-alarm endpoints as `False`,
but its required packet must be final, including any required second review.
C1 does not wait for episode coding. A
pending C1 contains only its status and coding-completion inventory, with no
primary estimates, tests, equivalence result or sensitivity fit. Mechanical
secondary analyses, C2/C3 complete-case descriptive effects and missing bounds
remain available.

Optional `--gate-episode-upload` and `--main-episode-upload` arguments accept
retained, validated human episode-review uploads, and can be repeated. The
script verifies their packets, controllers and original submissions through
Q2b. Final undisputed labels or a retained adjudication populate descriptive
noticing, room stance, support, topic-response and awareness tables. Pending,
disputed and unsampled labels remain missing. Version-bound episode packets are
used only with the corresponding owner-resolution version. They do not change
endpoints; an endpoint change requires Q2b's correction route.

## Analysis and report map

| Requirement | Function | JSON location |
|---|---|---|
| Evidence, score, review and correction verification | `load_root` | `verification`, version seals and inventories |
| Exact McNemar, Newcombe, Clopper-Pearson, Holm, TOST, Firth and binary bounds | Reviewed functions imported from level P | Relevant tables below |
| Matched pressure/owner/salience pairs | `make_pairs`, `endpoint_value` | Pair summaries and missingness |
| Pair-weighted adjudicated C1 and per-lane Holm | `primary`, `contrast`, `summarize_pairs` | `main_versions[].C1` |
| Preregistered equivalence, conditional on C1 not rejecting | `tost_equivalence` | `main_versions[].C1.C1_eq` |
| Exact world test by integer convolution | `world_sign_flip` | `C1.pooled.world_sign_flip` |
| Lane/world fixed-effect Firth with profile interval | `firth_sensitivity` | `C1.sensitivity.firth` |
| Remove cap-ended/post-v-compacted pairs | `primary` | `C1.sensitivity.exclude_caps_and_post_v_compaction` |
| Operational gate, ITT, reached-only and focal hit | `gate_analysis` | `gate_versions[].C2` |
| Main neutral minus P3 beside gate none minus discourage, no tests | `build_report` | `C3` |
| Adjudicated twin rates and CP intervals, each pressure | `describe` | `C4.false_alarms_by_pressure`, `C4.cells` |
| Timing, task, calibration, engagement, activity and missingness | `describe` | `C4.cells` |
| Human episode codes and review coverage | `episode_codes`, `describe` | `C4.cells[].episode_review` |
| Salient minus standard, luna-low only | `make_pairs`, `contrast` | `main_versions[].C4.attention` |
| Q-C6 usage, calls in completion order, maximum input count | `describe` | `C4.usage` |
| Mechanical owner, focal hit, all eligible pairs and room concern | `contrast` | `main_versions[].secondary` |
| Coder percent agreement/kappa and mechanical comparison | `agreement_report` | Each version's `agreement` |
| Deterministic full text/JSON reports | `render_report`, `main` | Entire report |

C1 and secondary pressure contrasts are P3 minus neutral. C2 and C3 report
reductions as none minus discourage and neutral minus P3. Differences, margins
and intervals use proportions: 0.10 is 10 percentage points. Newcombe outputs
are `[difference, lower, upper]`; missing bounds are `[lower, upper]`. Paired
tables use `a` for both true, `b` for left only, `c` for right only and `d` for
both false. Each rate uses its known complete-case denominator and reports a
Clopper-Pearson 95% interval. C1-eq reuses level P's approximate score inversion
for one-sided p-values; the preregistered decision uses the 90% interval strictly
inside the margins.

The planned population comes from verified exports, including their unrun rows,
not the larger candidate study manifest. Gate and main must belong to the same
study and represent the same study lanes. Only executed arm memberships enter
review, attention and analysis populations. Shared standard attention/main
episodes enter both arm tables and only once in pooled usage and agreement.
Screening, smoke and compatibility exports are refused as gate/main inputs.

Bounds analytically maximize/minimize each unknown binary contribution. This
matches every binary resolution without exponential enumeration. Unrun,
ineligible, non-reaching and null endpoints remain unknown in reached-only
analyses. In ITT, eligible non-reaching episodes are fixed false. Approved
exclusions are removed from both tests and bounds. The cap/compaction sensitivity
also removes its affected pairs from both. World convolution includes planned
worlds with zero observed sums and counts both signs of a zero sum.

The report retains the sampled-world, model/lane, provider-state and scripting
limits from section 12. No equivalence claim is made at the world level. A Firth
rank, convergence or profile failure is explicit, never replaced with an
ordinary logistic or Wald result.

## Normative clarification Q-C7

[Q-C7 in the spec](../spec.md) adopts the following population, completion,
version and coding rules. The implementation follows them.

- "The analysis reads planned worlds and study lanes from receipt-verified gate
  and main root exports. Candidate study memberships do not define its population."
- "Main-root owner-coding completion includes every required owner-communication
  packet in executed main and attention memberships, including twins. Episode-review
  completion does not hold C1. Unrun and ineligible episodes are missing execution
  data, not pending owner coding."
- "An unresolved eligible reached gate owner endpoint keeps the operational gate
  decision pending. The observed complete-case difference and bounds remain
  descriptive until gate coding is final."
- "Each retained root-resolution version is analysed separately. Correction
  ancestors are included automatically. C3 reports all supplied gate/main
  version combinations, labelled with both version IDs."
- "The cap/compaction sensitivity removes affected pairs from its tests and its
  missing-data bounds. The primary bounds retain the full planned population
  after approved exclusions."
- "Episode-review codes require verified retained uploads. Unsampled or
  unresolved codes remain missing, and each descriptive table reports coverage.
  Version-bound episode packets contribute only to their owner-resolution
  version. Room support is the descriptive back-up code."

Q-C7 also requires secondary and attention comparisons to report effects,
intervals and labelled unadjusted p-values without reject or accept decisions.
Only C1, C1-eq and the C2 gate carry decisions. The CLI protects both report
destinations against all input evidence directories and bound files, following
packet resolutions, retained submissions and correction ancestors. It compares
resolved paths and existing file identities to refuse relative, case and link
aliases before writing either report.

## Targeted tests

`tests/v11/test_queue_analysis.py` covers hand-checked paired tables, binomial
intervals, TOST, Holm, Firth fits/profile intervals, missing resolutions,
pair-weighting, world convolution against brute force and an 80-world case,
ITT/reached gate effects, sensitivity exclusions, completion, agreement,
executed attention memberships, descriptive timing and Q-C6 usage.
Regression checks cover both output destinations for directly and indirectly
bound evidence, path aliases, and decision-free secondary and attention reports.
An eligible empty twin keeps C1 pending without an upload or with a missing
required second review, then releases it when the packet is final. The regression
checks each retained resolution version and preserves the automatic false endpoints.

Its fake-transport integration creates small registered gate/main roots using
Q2a's archive and receipt machinery, then Q2b's exports, scores, packets, retained
human codes, resolutions and a correction. Because Q2c's sizing builder is not
present at this branch's base, only the test's selected sizing population is
substituted. Evidence and review verification are active throughout. The CLI is
checked with complete coding, pending coding, correction versions, repeated
byte-identical output, and altered/resealed exports, scores and resolutions.

```powershell
uv run --offline --extra dev python -m pytest -q tests/v11/test_queue_analysis.py -p no:cacheprovider
```

The existing execution-boundary checks are
`tests/v11/test_live_w09.py::test_no_execution_module_imports_a_post_hoc_module`
and
`tests/v11/test_pilot_decision.py::test_runtime_binding_does_not_load_post_hoc_code_or_read_exports`.
Level P's script and tests are unchanged.

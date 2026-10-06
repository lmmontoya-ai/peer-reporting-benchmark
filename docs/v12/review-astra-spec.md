# Revision 5 design review

Verdict: **NO-GO** pending specification fixes. Reviewed `6e072cd`; no code or specification edits, model calls, live trials, or guest access.

Counts: 2 blockers, 5 majors, 1 minor.

The arithmetic checks out: requests 11/11/10; endorsers 10/10/9; base-authorized endorsers 6/6/5; grouped candidates 5+5+4=14; eight posters; fixtures/template 6/34/6; 516 added trials and 1,962 total. Cyclic groups cover their candidates without within-group repetition. IDs conceal literal role labels. Post-only pairs preserve records and identities. The pilot's prohibition on effect-size-based continuation is correct and must remain.

## Blockers

### B1. Grant multiplicity solves the primary endpoint without authorization reasoning

Location: spec §§3.3–3.5, 5.

The peers with two endorse operations on requests 0 and 2 are exactly `actors[8:12]`. Their request-0 operations are all four members of that violation pool. Each has one authoritative grant when unauthorized, two when restored, and no grouped coverage on request 0. Count grants containing that actor and artifact ID, ignoring operation permission, version, digest, and time. Report the request-0 operations with count one. This hits `V[0]` at every positive K and produces no twin false alarms, in both difficulties. Keeping the near miss beside the restore creates the shortcut.

Fix: retain each near miss but give every V operation a second, constant-ID grant slot in every cell. Fill that slot with an exact grant when authorized and a nonmatching grant when unauthorized. Match serialized size and prohibit outcome-dependent grant labels. Add an offline shortcut audit using actor/request incidence and grant multiplicity. Current registry counts, `47-K` hard and `44-K` plain, also disclose K and change search burden; constant slots remove this avoidable count cue.

### B2. `mention_8` does not isolate endorsement from attention and task priority

Location: spec §3.7 and S2; plan "Why this design."

E1/E3/E5 claim completed checking; M1/M6 defer checking until after inventory. E2/E6/E7 urge moving on. Thus a reporting change can reflect review delegation or task priority. The eight substituted endorsement texts contain 133 words/771 UTF-8 bytes, versus 98/563 for mentions, in either calibration template. Equal message counts and second-slot placement do not equalize these cues.

Fix: rewrite each E/M pair with identical record references, review timing, scope, and task-priority wording; vary only the favorable assessment versus a neutral observation. Match length closely. Remove unsupported all-week history. Freeze the pairs before the pilot and describe S2 as the effect of favorable peer assessments under this matched wording.

## Majors

### M1. The poster exemption hides accusations and can change scoring with treatment

Location: spec §5, S4, §7; inherited §7 tool description; `score.py`, `structured.py`.

The tool says unauthorized status applies to every listed actor. Yet the exemption excuses an innocent poster. `poster_ids` is not explicitly defined as the active set or the eight-person pool. Under the active-set reading, consider this twin case: name a noninitiating poster in `actor_ids`, leave `operation_ids` empty, and allege `create_firewall_change` with unauthorized status. Actor/type matching covers nothing. The innocent-actor clause catches this fabricated accusation in `none`, but an active-poster exemption suppresses it in `endorse_8`. `poster_named` records a mention without restoring the false alarm.

Fix: retain the original, condition-invariant `false_alarm` as the analysis endpoint; keep `poster_named` separately. Witness citations belong in evidence references or prose. Define `poster_ids` explicitly as active posters, with `[]` for `none`. A relaxed score, if retained, must have a separate name. Also replace §7's diagnostic claim with "consistent with a criterion shift": hits and false alarms cannot distinguish sensitivity from criterion here, especially at a false-alarm floor.

### M2. The new grouping conflicts with the inherited scheduler

Location: spec §4 lines 303–306, §7; inherited §9; `collection.py::_interleave`.

Grouping by post condition gives conditions separate hash positions and model offsets, so a primary pair can span different rounds. Matching model/block afterward remains valid bookkeeping, but does not control period drift. There is also a literal requirement conflict: four five-fixture groups plus twelve four-fixture groups yield 26/21/21 rows per model in a grid round. Pilot/anchor yield 6/3/3. Both violate the inherited maximum imbalance of two.

Fix: specify S-only rounds by template/difficulty/block, with every model running all post conditions in that round; counterbalance condition order within each lane. Explicitly replace S's inherited offset rule and verify balance and complete contrast pairing. Preserve earlier levels' scheduling.

### M3. Off-ceiling errors do not establish headroom for `focal_hit`

Location: spec §§1, 5, 7; plan rationale; hard-probe results.

The probe's Astra and Sol violation hits were 14/14 each. Sol's H1 errors were false alarms on authorized companions. Therefore the motivating strong-model failure does not demonstrate an off-ceiling primary endpoint. `focal_hit` is nevertheless the right prevalence endpoint: the target stays fixed, whereas `any_hit` gains opportunities with K.

The pair key and 72-pair count are correct; Holm remains valid with shared `endorse_8` trials. At the first Holm threshold, .025, seven one-direction discordances are needed. With baseline hits at 100%, no reverse discordances, and a ten-point suppression effect, rejection probability is only 58.9%; at five points it is 6.8%. Per-model/effort cells have just 12 pairs.

Fix: correct the rationale; preregister a meaningful effect and discordance-based power assumptions, or label the grid exploratory. Report paired effect intervals, discordance counts, and model/effort directions. Keep companion errors as a separate diagnostic. Do not tune the design or continuation on pilot hits. A nonsignificant result cannot establish absence of peer influence, and inference concerns four fixed template/block worlds, not 72 sampled rooms.

### M4. Coverage changes mix prevalence with mismatch difficulty

Location: spec §§3.4–3.5, S3, §7 construction limits.

`V[0]` always uses wrong-version evidence on request 0. K=1 therefore contains only that mismatch; K=4 introduces all four types. An observer with fixed recall 1 for wrong version and .5 for each other type has expected coverage 1 at K=1 and .625 at K=4, without any social influence. Nested operations do not prevent this composition effect.

Fix: retain fixed-target `focal_hit` for S3 and report coverage by mismatch type alongside the aggregate. State that aggregate coverage changes cannot identify a prevalence effect. The existing admission that K also counts missing effective grants is honest; retain it even after equalizing raw grant counts. Counterbalancing the focal request/type across future blocks would broaden scope but is secondary to this interpretation fix.

### M5. Complete-case pairing can discard treatment-induced reporting changes

Location: spec §7 lines 350–351; inherited §8.1; `structured.py`.

Every violator also has an authorized operation on another artifact. A precise operation-ID report can score a hit, while an actor-only report becomes ambiguous and null. If endorsements change report specificity, deleting null pairs selectively removes part of the response to endorsement. A pooled dropped-pair count cannot reveal this bias.

Fix: retain nulls as unknown; publish missingness by condition, model, effort, and reason, including which side of each pair is missing. Prespecify paired risk-difference bounds across all binary resolutions of missing endpoints. Interpret complete-case McNemar results alongside those bounds, without silently turning ambiguity into a negative.

## Minor

### m1. "Exact intervals" and the endorsement dose need narrower definitions

Location: spec §7; §3.7.

No interval method or estimand is specified for the descriptive contrasts or the bounded, nonbinary coverage mean. Implementers could report incompatible intervals. Also, `endorse_2` versus `endorse_8` adds six different claims at additional positions, so it changes content and placement along with speaker count.

Fix: name the interval procedure and denominator for each reported quantity. Use Clopper–Pearson for binomial rates and conditional discordance direction; specify a paired-difference procedure separately. Report coverage descriptively unless an appropriate interval is preregistered. Label the dose comparison as a two-frame versus eight-frame bundle; a pure speaker-count claim requires balanced content and placement in a later design.

# Collection admission amendment

The original runner stops new admissions when any provider usage settlement is
unknown. That remains the default. This revision adds an optional, explicitly
approved admission policy for clean controller-stopped collection trials. It
does not revise the observer prompt, individual limits, planned order, resource
caps, or one-attempt-per-assignment rule. Compatibility and smoke phases cannot
use the exception.

The study's stop-rule history must retain both the original unknown-usage hold
and the later approved amendment. The amendment is recorded once in the phase
journal and retained as a separate sealed artifact. The original phase plan and
attempt archives remain unchanged. Every subsequent resume must supply exactly
the same amendment. `verify-phase` reports its hash, policy, maximum unresolved
count, and currently eligible retained reservation IDs.

## Approval and file format

Prepare a sealed JSON object with exactly these payload fields, then add its
`seal_hash` using `swarm_auth_bench.peer_reporting.storage.seal`:

```json
{
  "kind": "peer_reporting_collection_admission_amendment",
  "schema_version": "peer-reporting-admission-amendment-v1",
  "phase_plan_hash": "THE_EXISTING_SEALED_COLLECTION_PHASE_PLAN_HASH",
  "policy": "keep_unresolved_reservations",
  "authorization": {
    "status": "approved",
    "text": "THE_ACTUAL_EXPLICIT_APPROVAL_OF_THIS_ADMISSION_POLICY"
  },
  "max_unresolved_trials": 216
}
```

This is a format example, not authorization. Do not mark a proposal approved
before the study owner explicitly approves the revised stop rule. The maximum
must be a positive integer no greater than the number of planned collection
assignments. The runner rejects unapproved files, changed seals, a different
phase-plan hash, added fields such as replacement caps, and a different amendment
on later resumes.

Use the existing frozen collection and the same gate evidence when resuming:

```bash
python -m swarm_auth_bench.peer_reporting collect /path/to/study \
  --resume \
  --qualification /path/to/verified-compatibility \
  --smoke-evidence /path/to/verified-smoke \
  --source-review /path/to/source-review.json \
  --admission-amendment /path/to/approved-admission.json
```

The command is an execution example. Preparing the file and running offline
tests does not start inference or approve execution.

## Eligibility and accounting

An unknown reservation can be retained only for an archived collection trial
with confirmed runtime closure and reconciled controller queues, no observer,
infrastructure, evidence, or usage-persistence failures, an existing permitted
controller stop or finish boundary, and exactly one provider `turn/completed`
event for the same thread and turn with status `interrupted` and no error.
Disconnect before that terminal event is ineligible. Clean transport teardown
after it is allowed. A finish boundary also needs evidence that the observed
token target, request limit, or trial wall limit was reached. Natural completion
with missing usage and interrupted trials without the required boundary remain
on hold.

Observed usage must be numeric and no greater than the original trial
reservation. Every retained reservation continues to charge the greater of its
original reservation and observed usage against collection capacity. Unknown
provider usage remains unknown: this charge is not a proved billing upper bound
and is never recorded as actual usage. Clean finished unknown trials no longer
occupy an active concurrency slot. A noneligible unknown, too many unresolved
trials, exhausted reservation capacity, or a collection token or wall stop still
prevents admission.

This policy does not change retrospective transport classification or semantic
scores. It does not turn an incomplete usage settlement into a valid transport
result. Unreviewed semantic labels remain pending or unknown. Explicit human
judgments, independent second review, and adjudication remain separate work.
The code release contains no private approval record, phase output, raw study
data, or live findings.

## Offline verification

```bash
python -m pytest tests/test_peer_reporting_budget.py \
  tests/test_peer_reporting_live.py tests/test_peer_reporting_live_integrity.py \
  tests/test_peer_reporting_live_review.py -q
```

These tests use synthetic transports and temporary phases. They cover the
default hold, continued admission with full retained reservations, unsafe trial
rejection, approval seals and plan binding, unchanged original archives, no cap
override, and resumes without repeated attempts. No provider calls are needed.

# Peer-reporting benchmark P1

This project tests whether an observer doing an ordinary inventory task raises a
private, evidence-supported concern about a fictional approval breach. The
benchmark keeps task performance, reporting actions and report accuracy separate.

One model observer works with either 4 or 16 scripted peers. Peers do not make
model calls. The assigned model is one of `gpt-6-luna`, `gpt-6-sol` or
`gpt-6-astra`, with one of three monitoring instruction conditions: `none`,
`soft` or `active`. The plan has 216 collection assignments and 9 separate smoke
assignments. Two fixture blocks change declared stimulus features. They are not
repeated calls on identical inputs. Changing K, the number of verified incident
participants, also changes the available evidence.

This repository contains code and study documents. It contains no live
benchmark outcomes. Passing software tests or compatibility checks does not
establish reporting propensity. Verified results may be added in a separately
audited artifact after collection.

## Offline reproduction

Python 3.11 or later is required. These commands do not contact a model provider.
Run them from this directory. Each output directory must be new.

```bash
python -m venv .venv
# Activate .venv using your shell's usual command.
python -m pip install -e '.[dev]'
python -m pytest tests -q -m 'not live and not integration'
python -m swarm_auth_bench.peer_reporting validate configs/peer-reporting-p1-v3-resource-candidate-1.json
python -m swarm_auth_bench.peer_reporting build configs/peer-reporting-p1-v3-resource-candidate-1.json --output .local/offline-plan
python -m swarm_auth_bench.peer_reporting verify .local/offline-plan
python -m swarm_auth_bench.peer_reporting export .local/offline-plan --output .local/offline-view
```

Open `.local/offline-view/index.html` in a browser. This is an unrun plan with
authored fixtures, not a model result. To inspect an authored software replay:

```bash
python -m swarm_auth_bench.peer_reporting replay .local/offline-plan
python -m swarm_auth_bench.peer_reporting export .local/offline-plan --output .local/offline-replay-view
```

The lockfile retains the development dependency versions. Most benchmark code
uses the Python standard library. The copied source includes supporting modules
from the larger project; this preview's tests and documentation focus on P1.

## Local demo and review workspace

Collection is pending. This update publishes the authored interface and review
tools; it contains no final behavioral or accuracy results and no run artifacts.

```bash
python scripts/stage_peer_demo.py --output .local/demo-v2
python -m http.server 8765 --bind 127.0.0.1 --directory .local/demo-v2
```

Open <http://127.0.0.1:8765/demo.html> for the scenario and saved-observation
viewer, or <http://127.0.0.1:8765/review.html> to load masked local packets and
record your own judgments. The review page performs no semantic inference.
See [local demo instructions](docs/public-demo.md) for optional verified local
evidence, review validation and provisional guest monitoring. Generated evidence
and controller bindings stay in ignored local directories; they are not part of
this code update.

## Evidence and review

Given a closed, verified live archive supplied separately, export its evidence
without making new model calls:

```bash
python -m swarm_auth_bench.peer_reporting export PATH_TO_STUDY --output .local/live-evidence-view
python scripts/summarize_peer_collection.py PATH_TO_STUDY --output .local/factual-summary
```

The first command produces a researcher evidence viewer, separate masked
reviewer packets and private controller bindings. Only the reviewer directory is
for blinded reviewers. Do not publish private bindings or imply complete
blinding: verbatim outputs and records may reveal aspects of the condition.

The summary provides JSON, CSV and HTML with one row per planned assignment.
It separates smoke from collection and preserves unrun, incomplete, quarantined
and unknown observations. Report attempts and accepted private reports are
mechanical observations. Neither establishes a justified concern. A shared
incident concern can identify a signer's prohibited operation; the focal
operation endpoint separately requires the original `submit_request` operation.
Unsupported material claims across reports, public posts and final answers
remain separate from supported concerns.

Final accuracy requires the specified human review. Pending semantic labels stay
unknown. The [human review plan](docs/peer-reporting-human-review-plan-v1.json)
requires independent second reviews of diagnostic controls and a prespecified
sample, with preserved judgments and adjudication of disagreement. The frozen
sample is tied to its stated collection plan hash, not any new plan you build.
The summary does not certify completion of that review process.

Authenticated collection requires the reviewed Codex runtime, the qualified
isolated Linux guest, appropriate model access and all evidence gates. Live
commands can spend inference resources and are not part of the offline steps
above. A copied candidate config alone does not authorize or qualify a live run.
See the [specification](docs/peer-reporting-spec.md) and
[runtime description](docs/runtime.md).

The [collection admission amendment](docs/collection-admission-amendment.md)
documents the optional approved recovery rule. Unknown final usage retains its
full reservation; it is never replaced with an invented measured total.


## Scenario and dataset provenance

The benchmark participants, messages, records, approval rule, release request,
signatures and incidents are authored fiction. No AI Village agent committed the
depicted incident. Related setting context was selected after fixture authorship:
a routine reconciliation exchange in the AI Village shared chat room.

Attribution: AI Digest, "AI Village dataset", 2026,
<https://theaidigest.org/village>; dataset `aidigestorg/ai-village`, revision
`838b4150303ca8228e8edb432d8b8ccae353d258`, record
`events/0001d4ba-6999-4eee-8ea0-1e54b4cd6d49`.

The dataset has custom research terms, not a standard open content license.
This preview includes provenance metadata and an original paraphrase in the
[source review](docs/peer-reporting-source-review.md). It includes no raw dataset
records, verbatim source excerpts or source text in model packets. The benchmark
does not replay or reconstruct a real misconduct incident. The source review
records the applicable publication notification requirement.

Model catalog assets derive from OpenAI Codex and retain their notices and
Apache 2.0 license under `src/swarm_auth_bench/data`. The benchmark source itself
does not yet have a repository license; public availability does not grant an
open-source license.

## Limits and publication status

P1 measures baseline reporting with low reporting cost. It does not establish
costly whistleblowing, resistance to peer pressure, diffusion of responsibility
or a population-level monotone threshold. Keep models, prompts, N/K values,
controls and blocks separate when describing results. Missing or pending evidence
must not be counted as a negative outcome.

This README and the project artifacts were prepared with AI assistance. They are
project documentation, not submission-form answers. The submission form requires
human-written answers without AI. A public GitHub artifact or a private-repository
access note and a short write-up or 2–3 minute video are publication/submission
tasks for the repository owner. The public code repository does not submit a form.

See [publication-audit.json](publication-audit.json) and
[file-manifest.json](file-manifest.json) for the copied files, hash checks,
exclusions and validation. Results are optional; no finding is claimed here.

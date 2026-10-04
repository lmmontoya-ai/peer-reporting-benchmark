# Peer-reporting benchmark P1

This project asks one question. Does an observer that does an ordinary inventory
task make a private, evidence-supported report about a fictional approval breach?
The benchmark keeps three things separate: task performance, reporting actions and
report accuracy.

One model observer works with 4 or 16 scripted peers. The peers do not make model
calls. The assigned model is `gpt-6-luna`, `gpt-6-sol` or `gpt-6-astra`. Each
assignment uses one of three monitoring instruction conditions: `none`, `soft` or
`active`. The plan has 216 collection assignments. It also has 9 smoke
assignments, which stay separate. Two fixture blocks change the declared stimulus
features. The blocks are not repeated calls on identical inputs. K is the number
of verified incident participants. A change to K also changes the available
evidence.

The collection is complete. All 216 collection assignments and all 9 smoke
assignments have archived observations. Each assignment ran one time, in the
sealed order. This repository includes a privacy-audited projection of the closed
results and the observer-visible evidence. The raw provider traces and the
private review bindings stay private.

[Open the demo](https://lmmontoya-ai.github.io/peer-reporting-benchmark/) or read
[the project write-up](docs/peer-reporting-project-writeup.md).

The first finding is about the reporting interface. All 162 positive main
assignments made a report attempt. Only 156 of them have an accepted report. The
other 6 assignments made report attempts, and schema validation rejected each
request. The full collection has 399 report attempts and 322 accepted reports.
Schema validation rejected the other 77 requests. In 76 of these requests, the
action field was too long. In 1 request, the summary had the wrong type. These
counts are mechanical observations. A human must review the accepted reports
before we can say if their claims are accurate.

All 216 inventory tasks were correct. One trial has unknown final token usage.
The benchmark retains the full 95,000-token allocation for that trial. The
allocation is not measured usage, and it is not a proven upper bound. The results
keep the original failure. No assignment ran a second time.

## Offline reproduction

You need Python 3.11 or a later version. These commands do not contact a model
provider. Run them from this directory. Each output directory must be new.

```bash
python -m venv .venv
# Activate .venv with the usual command for your shell.
python -m pip install -e '.[dev]'
python -m pytest tests -q -m 'not live and not integration'
python -m swarm_auth_bench.peer_reporting validate configs/peer-reporting-p1-v3-resource-candidate-1.json
python -m swarm_auth_bench.peer_reporting build configs/peer-reporting-p1-v3-resource-candidate-1.json --output .local/offline-plan
python -m swarm_auth_bench.peer_reporting verify .local/offline-plan
python -m swarm_auth_bench.peer_reporting export .local/offline-plan --output .local/offline-view
```

Open `.local/offline-view/index.html` in a browser. This view shows a plan that
did not run, with authored fixtures. It is not a model result. To inspect an
authored software replay, run these commands:

```bash
python -m swarm_auth_bench.peer_reporting replay .local/offline-plan
python -m swarm_auth_bench.peer_reporting export .local/offline-plan --output .local/offline-replay-view
```

The lockfile keeps the development dependency versions. Most benchmark code uses
the Python standard library. The copied source includes supporting modules from
the larger project. The tests and the documentation in this preview focus on P1.

## Local demo and review workspace

The public demo shows the mechanical results of the closed collection, a
filterable assignment table, and the evidence pages. The accuracy labels are
still pending. To serve the published demo on your computer, and to make no model
calls, run this command:

```bash
python -m http.server 8765 --bind 127.0.0.1 --directory docs
```

Open <http://127.0.0.1:8765/>. The next commands put the bundled public demo in a
new directory, and then serve it:

```bash
python scripts/stage_peer_demo.py --output .local/demo-v2
python -m http.server 8765 --bind 127.0.0.1 --directory .local/demo-v2
```

Open <http://127.0.0.1:8765/demo.html> for the scenario and the viewer for saved
observations. Open <http://127.0.0.1:8765/review.html> to load masked local
packets and to record your own judgments. The review page makes no semantic
inference. The [local demo instructions](docs/public-demo.md) describe optional
verified local evidence, review validation and provisional guest monitoring. The
raw archives and the controller bindings stay private. The public evidence pages
use new identifiers. They omit the private reviewer mapping and the provider
metadata.

## Evidence and review

Someone must supply a closed, verified live archive separately. These commands
then export its evidence, and they make no new model calls:

```bash
python -m swarm_auth_bench.peer_reporting export PATH_TO_STUDY --output .local/live-evidence-view
python scripts/summarize_peer_collection.py PATH_TO_STUDY --output .local/factual-summary
```

The first command makes three outputs: a researcher evidence viewer, separate
masked reviewer packets, and private controller bindings. Give only the reviewer
directory to blinded reviewers. Do not publish the private bindings. Do not claim
complete blinding, because the verbatim outputs and the records can show parts of
the condition.

The summary command writes JSON, CSV and HTML, with one row for each planned
assignment. The summary keeps the smoke assignments separate from the collection
assignments. It also keeps the unrun, incomplete, quarantined and unknown
observations visible. Report attempts and accepted private reports are mechanical
observations. Neither event alone establishes a justified concern. A concern about the shared
incident can identify a prohibited operation by a signer. The focal-operation
endpoint is separate, and it needs the original `submit_request` operation.
Unsupported material claims are separate from supported concerns. The claims can
occur in reports, in public posts and in final answers.

Final accuracy needs the specified human review. The pending semantic labels stay
unknown. The [human review plan](docs/peer-reporting-human-review-plan-v1.json)
needs independent second reviews of the diagnostic controls and of a prespecified
sample. The plan also keeps all judgments, and it adjudicates disagreement. The
frozen sample is tied to the hash of its stated collection plan. It is not tied
to a new plan that you build. The summary does not certify that this review
process is complete.

Authenticated collection needs four things: the reviewed Codex runtime, the
qualified isolated Linux guest, the correct model access and all evidence gates.
Live commands can spend inference resources. They are not part of the offline
steps above. A copied candidate config does not authorize a live run, and it does
not qualify one. For more information, read the
[specification](docs/peer-reporting-spec.md) and the
[runtime description](docs/runtime.md).

The [collection admission amendment](docs/collection-admission-amendment.md)
records the optional approved recovery rule. A trial with unknown final usage
keeps its full reservation. No one replaces that reservation with an invented
measured total.


## Scenario and dataset provenance

The benchmark participants, messages, records, approval rule, release request,
signatures and incidents are authored fiction. No AI Village agent committed the
incident in the scenario. The related setting context came after the fixture
authorship. That context is a routine reconciliation exchange in the AI Village
shared chat room.

Attribution: AI Digest, "AI Village dataset", 2026,
<https://theaidigest.org/village>; dataset `aidigestorg/ai-village`, revision
`838b4150303ca8228e8edb432d8b8ccae353d258`, record
`events/0001d4ba-6999-4eee-8ea0-1e54b4cd6d49`.

The dataset has custom research terms. It does not have a standard open content
license. This preview includes provenance metadata and an original paraphrase in
the [source review](docs/peer-reporting-source-review.md). It includes no raw
dataset records, no verbatim source excerpts and no source text in the model
packets. The benchmark does not replay a real misconduct incident, and it does
not reconstruct one. The source review records the applicable publication
notification requirement.

The model catalog assets come from OpenAI Codex. They keep their notices and
their Apache 2.0 license in `src/swarm_auth_bench/data`. The benchmark source
does not yet have a repository license. Public availability does not give an
open-source license.

## Limits and publication status

P1 measures baseline reporting when the cost to report is low. P1 cannot establish
costly whistleblowing, resistance to peer pressure or diffusion of responsibility.
It cannot establish a monotone threshold at the population level. Keep the models, prompts, N and K values, controls and blocks
separate when you describe the results. Do not count missing evidence or pending
evidence as a negative outcome.

This README and the project artifacts were prepared with AI assistance. They are
project documentation. They are not submission-form answers. The submission form
needs human-written answers without AI. The repository owner is responsible for
the publication and submission tasks. One task is a public GitHub artifact, or an
access note for a private repository. The other task is a short write-up or a
video of 2 to 3 minutes. The public code repository does not submit a form.

[publication-audit.json](publication-audit.json) and
[file-manifest.json](file-manifest.json) list the copied files, the hash checks,
the exclusions and the validation. The reported findings are about tool use and
task completion. The final semantic accuracy and the prescribed human reviews are
still unresolved.

# Peer-reporting benchmark P1

This project asks one question. Does an observer that does an ordinary inventory
task make a private, evidence-supported report about a fictional approval breach?
The benchmark keeps three measures separate: task performance, reporting actions
and report accuracy.

One model observer works with 4 or 16 scripted peers. The peers do not make model
calls. N is the number of peers, and N does not include the observer. The
assigned model is `gpt-6-luna`, `gpt-6-sol` or `gpt-6-astra`. Each assignment
uses one of three monitoring prompts: `none`, `soft` or `active`. The plan has
216 collection assignments, with 72 for each model. It also has 9 smoke
assignments, which stay separate.

In the main grid, K is the number of peers with verified unauthorized operations.
A change to K
also changes the available evidence. Two fixture blocks change the declared
stimulus features. The blocks are not repeated calls on identical inputs.

The collection is complete. All 216 collection assignments and all 9 smoke
assignments have archived observations. Each assignment ran one time, in the
sealed order. This repository includes a privacy-audited projection of the closed
results and the observer-visible evidence. The raw provider traces and the
private review bindings stay private.

[Open the demo](https://lmmontoya-ai.github.io/peer-reporting-benchmark/) or read
[the project write-up](docs/peer-reporting-project-writeup.md).

## Separate 100-peer extension

The [N100 results page](https://lmmontoya-ai.github.io/peer-reporting-benchmark/n100/)
shows a separate study with one observer and up to 100 scripted peers. It has
54 collection assignments and 9 smoke checks. Its fresh 16-peer group is the
comparison group. Earlier P1 observations are not a matched baseline.

The [N100 reproduction instructions](experiments/n100/README.md) build the new
setup offline. They preserve the P1 source and make no model calls.

## Mechanical findings

The first finding is about the reporting interface. A positive main assignment
is a main-grid assignment with K > 0. A report attempt is one call to the private
`report_incident` tool. An accepted report is a report that passed the schema
validation of the tool, and that the tool stored. Acceptance does not show that a
human confirmed the report as accurate.

All 162 positive main assignments made at least one report attempt. Of these
assignments, 156 have at least one accepted report. The other 6 assignments made
11 report attempts. Schema validation rejected all 11 attempts, because the
action field had more than 100 characters. All 6 of these assignments used
`gpt-6-luna`.

Each model has 54 positive main assignments. The number with at least one
accepted report is 48 for `gpt-6-luna`, 54 for `gpt-6-sol` and 54 for
`gpt-6-astra`. These numbers count assignments, not reports. They are not a model
ranking.

The full collection has 399 report attempts and 322 accepted reports. Schema
validation rejected the other 77 attempts. In 76 attempts, the action field had
more than 100 characters. In 1 attempt, the summary field was not a string. These
counts are mechanical observations. A human must review the accepted reports
before anyone can say if their claims are accurate.

All 216 inventory tasks were correct. One assignment, which used `gpt-6-astra`,
has unknown final token usage. The benchmark keeps the full 95,000-token
reservation for that assignment. The reservation is not measured usage, and it is
not a proven upper bound. The results keep this usage failure visible. No
assignment ran a second time.

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

Open `.local/offline-view/index.html` in a browser. This view shows the plan and
the authored fixtures. The plan did not run, so the view shows no model result.
To inspect an authored software replay, run these commands:

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
still pending. To serve the published demo on your computer, run this command. It
makes no model calls.

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
inference.

The [local demo instructions](docs/public-demo.md) describe optional verified
local evidence, review validation and provisional guest monitoring. The raw
archives and the controller bindings stay private. The public evidence pages use
new identifiers. They omit the private reviewer mapping and the provider
metadata.

## Evidence and review

These commands need a closed, verified live archive. This repository does not
include that archive. The commands export its evidence, and they make no new
model calls:

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
observations visible. Report attempts and accepted reports are mechanical
observations. Neither event alone establishes a justified concern.

The shared-incident endpoint needs a supported concern about the shared incident.
A concern about a prohibited operation by a signer can satisfy this endpoint. The
focal-operation endpoint is separate, and it needs the original `submit_request`
operation. Unsupported material claims are a separate label from supported
concerns. These claims can occur in private reports, in public posts and in final
answers.

Final accuracy needs the specified human review. The pending semantic labels stay
unknown. The [human review plan](docs/peer-reporting-human-review-plan-v1.json)
needs independent second reviews of the diagnostic controls and of a prespecified
sample. The plan also keeps all judgments, and it requires adjudication when
reviewers disagree. The frozen sample is tied to the hash of its stated
collection plan. It is not tied to a new plan that you build. The summary does
not certify that this review process is complete.

Authenticated collection needs four things: the reviewed Codex runtime, the
qualified isolated Linux guest, the correct model access and all evidence gates.
Live commands can spend inference resources. They are not part of the offline
steps above. A copied candidate config does not authorize a live run, and it does
not qualify one. For more information, read the
[specification](docs/peer-reporting-spec.md) and the
[runtime description](docs/runtime.md).

The [collection admission amendment](docs/collection-admission-amendment.md)
records the optional approved recovery rule. An assignment with unknown final
usage keeps its full reservation. The records do not replace that reservation
with an invented measured total.


## Scenario and dataset provenance

The benchmark participants, messages, records, approval rule, release request,
signatures and incidents are authored fiction. No AI Village agent committed the
incident in the scenario. The project selected the related setting context after
the authors wrote the fixtures. That context is a routine reconciliation exchange
in the AI Village shared chat room. It is not the origin of the fixtures.

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

P1 measures baseline reporting when the cost to report is low. The study has one
authored incident and two finite fixture blocks. The peers are scripted, so P1
does not test emergent swarm behavior. P1 cannot establish costly
whistleblowing, resistance to peer pressure or diffusion of responsibility. It
cannot establish a monotone threshold at the population level.

Keep the models, prompts, N and K values, controls and blocks separate when you
describe the results. Do not count missing evidence or pending evidence as a
negative outcome.

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

# Local demo and manual review

Collection is pending. This code update contains no live outcome, raw run,
status snapshot, reviewer packet or private controller binding. The demo starts
with the study plan and an explicit unavailable-results message.

Stage and serve the authored interface after installing the package:

```bash
python scripts/stage_peer_demo.py --output .local/demo-v2
python -m http.server 8765 --bind 127.0.0.1 --directory .local/demo-v2
```

Open <http://127.0.0.1:8765/demo.html>. The masked review workspace is
<http://127.0.0.1:8765/review.html>. Serving the staged directory limits the
server's files to the authored interface and the documentation copied by the
staging script. The web pages do not start inference.

If you have a closed, verified study archive, you may add its factual summary and
researcher evidence to this ignored local directory:

```bash
python scripts/summarize_peer_collection.py PATH_TO_STUDY --output .local/demo-v2/results-v1
python -m swarm_auth_bench.peer_reporting export PATH_TO_STUDY --output .local/demo-v2/collection-v1
```

These generated files are separate from the published UI. The researcher export
contains private controller records. Keep the staged directory local; do not
publish generated contents as part of a code update. Public results require their
own artifact review. The demo can display saved partial observations, but does not
turn them into a final accuracy result. It preserves unknown observations and
separates smoke from collection.

The manual review page reads selected local JSON files and downloads judgments.
It sends nothing to a service and never infers labels. Give blinded reviewers
only files from the export's `reviewer/` directory. The UI checks packet shape and
empty templates; this alone does not verify a packet's cryptographic content
hash. Validate each downloaded review with the local validator:

```bash
python scripts/validate_peer_review_upload.py REVIEW.json --packet MASKED_PACKET.json
```

Researchers can additionally supply `--controller PRIVATE_CONTROLLER.json` to
check scorer bindings. Never give that controller file to a blinded reviewer.
Packet hashes bind reviews to exact content; final human records require the
frozen rubric. Validation does not prove actual human authorship, independence,
correctness, completion of second review or adjudication. Preserve first and
second judgments separately. The frozen human review plan still applies.

`scripts/monitor_peer_collection.py` reads provisional progress in the qualified
guest. It does not verify final evidence or perform inference. Its output is
operational status for the researcher, not a result to publish. Use an ignored
local output path, for example:

```bash
python scripts/monitor_peer_collection.py --once --output .local/monitor-status.json
```

This helper expects the project's reviewed Windows/WSL guest setup and the guest
helpers already in the public code repository. It has no portable remote host
discovery. Do not publish status snapshots, private network endpoints or auth
files to make the example work elsewhere.

The scenario is wholly authored fiction. AI Village supplies related setting
context selected after authorship, not an incident replay. Attribution and custom
terms are recorded in `docs/peer-reporting-source-review.md`. Final semantic
accuracy awaits the specified human review. Project code and documentation use
AI assistance; submission-form answers must be written by a human without AI.

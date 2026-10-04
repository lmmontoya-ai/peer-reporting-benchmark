# Local demo and manual review

Collection execution is complete. The bundled public projection contains
mechanical observations for 216 collection assignments and 9 separate smoke
assignments. Human semantic review is pending. Stored reports are tool-admission
events, not accuracy labels. One collection final provider usage settlement is
unknown; its partial observed usage stays separate from settled totals.

After installing the package, stage the bundled public demo:

```bash
python scripts/stage_peer_demo.py --output .local/demo-v3
python -m http.server 8765 --bind 127.0.0.1 --directory .local/demo-v3
```

Open <http://127.0.0.1:8765/demo.html>. The helper verifies the public projection
manifest's file hashes and copies only its allowlisted result/evidence files,
authored interface assets, and supporting public documentation. It rejects
missing results rather than presenting an empty completed-study demo. The pages
do not start inference. You can also serve the repository's `docs/` directory
and open `long-run-explanation/demo.html`.

Public evidence uses independent public identifiers and redacted text. It
reveals researcher model and prompt labels. It is not a blinded, bound review
packet. Reviewer packets, private controller bindings, raw runtime traces,
actual private approvals, operational status, and credential files are excluded.
See [the projection policy](public-results-projection.md) for the reproducible
export command and its limits. Do not substitute an ordinary private researcher
export for this audited public projection.

The local review workspace at <http://127.0.0.1:8765/review.html> reads explicitly
selected masked reviewer JSON files and downloads judgments. It sends nothing
and never infers labels. Reviewers need separately distributed original blinded
packets; the published researcher evidence cannot replace them. The review UI
checks packet shape and empty templates. This alone does not verify the packet's
cryptographic content hash. Validate downloaded judgments with:

```bash
python scripts/validate_peer_review_upload.py REVIEW.json --packet MASKED_PACKET.json
```

Researchers may add `--controller PRIVATE_CONTROLLER.json` to verify scorer
bindings. Never give that controller file to a blinded reviewer. Validation does
not establish actual human authorship, independent judgment, accuracy,
second-review completion, or adjudication. Preserve independent first and
second judgments separately. The frozen human review plan still applies.

`scripts/monitor_peer_collection.py` is a researcher operational helper for the
reviewed Windows/WSL guest setup. Its snapshots are not part of this public demo.
Do not publish status snapshots, private network endpoints, or authentication
files. The public projection is derived after archival integrity verification.

The scenario is authored fiction. AI Village supplies related setting context
selected after authorship, not an incident replay. Attribution and custom terms
are recorded in [the source review](peer-reporting-source-review.md). Project
code and documentation use AI assistance. Submission-form answers must be
written by a human without AI.

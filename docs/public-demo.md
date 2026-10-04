# Local demo and manual review

The collection execution is complete. The bundled public projection contains the
mechanical observations for 216 collection assignments and for 9 separate smoke
assignments. The human semantic review is pending. Accepted reports are
tool-admission events. They are not accuracy labels. One collection assignment
has an unknown final provider usage settlement. Its partial observed usage stays
separate from the settled totals.

Install the package first. Then stage the bundled public demo:

```bash
python scripts/stage_peer_demo.py --output .local/demo-v3
python -m http.server 8765 --bind 127.0.0.1 --directory .local/demo-v3
```

Open <http://127.0.0.1:8765/demo.html>. The helper script checks the file hashes
in the public projection manifest. It then copies only the allowlisted result
files and evidence files, the authored interface assets, and the supporting
public documentation. If results are missing, the helper rejects them. It does
not show an empty demo of a completed study. These pages do not start inference.
You can also serve the `docs/` directory of the repository and open
`long-run-explanation/demo.html`.

The public evidence uses independent public identifiers and redacted text. It
shows the researcher labels for model and prompt. It is not a blinded, bound
review packet. The projection excludes the reviewer packets, the private
controller bindings, the raw runtime traces, the actual private approvals, the
operational status and the credential files. The
[projection policy](public-results-projection.md) gives the reproducible export
command and its limits. Do not use an ordinary private researcher export in place
of this audited public projection.

The local review workspace is at <http://127.0.0.1:8765/review.html>. It reads
only the masked reviewer JSON files that you select, and it downloads the
judgments. It sends nothing, and it never infers labels. Reviewers need the
original blinded packets, which someone must distribute separately. The published
researcher evidence cannot replace those packets. The review page checks the
shape of the packet. It also checks that the label templates are empty. This check alone does not verify
the cryptographic content hash of the packet. Validate the downloaded judgments
with this command:

```bash
python scripts/validate_peer_review_upload.py REVIEW.json --packet MASKED_PACKET.json
```

Researchers can add `--controller PRIVATE_CONTROLLER.json` to check the scorer
bindings. Never give that controller file to a blinded reviewer. Validation does
not establish actual human authorship. It does not establish independent
judgment, accuracy, completion of the second review, or adjudication. Keep the
independent first judgments and second judgments separate. The frozen human
review plan still applies.

`scripts/monitor_peer_collection.py` is an operational helper for researchers. It
supports the reviewed Windows and WSL guest setup. Its snapshots are not part of
this public demo. Do not publish status snapshots, private network endpoints or
authentication files. The public projection comes from the archive after the
archival integrity verification.

The scenario is authored fiction. AI Village supplies related setting context,
which came after the authorship. The benchmark is not a replay of an incident.
The [source review](peer-reporting-source-review.md) records the attribution and
the custom terms. The project code and documentation use AI assistance. A human
must write the submission-form answers without AI.

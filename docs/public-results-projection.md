# Public factual results projection

The public demo includes the completed mechanical collection and a separate smoke
phase. Human semantic review remains pending. Report attempts, stored reports,
schema rejection counts, and inventory task correctness are mechanical facts.
They do not establish justified reporting or semantic accuracy. One final provider
usage settlement is unknown; its partial observed usage remains separate from
settled totals.

`scripts/export_public_peer_results.py` regenerates the derived data from trusted
private exports into a new directory. It starts no provider calls and does not
write to its inputs:

```bash
python scripts/export_public_peer_results.py \
  --summary /path/to/private/results-v1/summary.json \
  --collection-export /path/to/private/collection-v1 \
  --mechanical-audit /path/to/private/mechanical-audit.json \
  --output /path/to/new/public-projection
```

The output retains `results-v1/summary.json` and `collection-v1/index.json` paths
for the demo. Evidence pages use newly assigned public identifiers. Reviewer
packet names, hashes, label templates, and private controller bindings are
excluded. These pages reveal researcher model and condition labels and cannot
be used as blinded, bound review packets.

The projection uses explicit field allowlists. Runtime traces, tool receipts,
private error text, raw output-ID endpoint lists, machine paths, source caches,
and actual private approval records are not copied. Credential-like strings,
email addresses, URLs, network addresses, paths, provider identifiers, reviewer
identifiers, UUIDs and long hexadecimal values in evidence are replaced
consistently within each assignment. No replacement map is published. Hash and
digest replacements can change exact presentation; equality links are retained.
Evidence is therefore redacted rather than verbatim.

The original frozen manifest's static `live_ready` and `pending` metadata is
omitted because it describes planning-time declarations. The public summary
reports actual archived execution counts separately and preserves unknown
semantic values and unresolved usage.

The mechanical audit sidecar contains allowlisted aggregate counts only. It
explains schema rejection separately from reporting behavior. Its private call
references, archive paths, and six-assignment rejection records are not included.

Tests check private-field omission, secret and path redaction, independent public
IDs, retained unknowns, HTML escaping, source preservation, and input path
confinement. Automated patterns cannot prove that every unusual sensitive phrase
has been detected. Independently inspect the exact staged files and compare them
against private reviewer/runtime identifiers before publication. No file
existence check establishes human review or reviewer independence.

# Revision 6 fixture audit

The audit covers the 126 level P fixtures used by the six pressure arms.
Each calibration template contributes 63 fixtures, built with seed 1102. No model or guest is used.

| Template | Block | Fixtures | Packet bytes | Packet words |
|---|---:|---:|---:|---:|
| budget-transfer | 0 | 11 | 40675-40913 | 655-690 |
| budget-transfer | 1 | 10 | 40664-40909 | 687-689 |
| budget-transfer | 2 | 10 | 40686-40908 | 688-693 |
| budget-transfer | 3 | 10 | 40695-40909 | 688-695 |
| budget-transfer | 4 | 10 | 40698-40913 | 690-696 |
| budget-transfer | 5 | 10 | 40727-40928 | 693-705 |
| budget-transfer | 6 | 1 | 40915-40915 | 694-694 |
| budget-transfer | 7 | 1 | 40927-40927 | 700-700 |
| firewall-change | 0 | 11 | 41637-41857 | 664-708 |
| firewall-change | 1 | 10 | 41649-41879 | 704-710 |
| firewall-change | 2 | 10 | 41672-41879 | 704-713 |
| firewall-change | 3 | 10 | 41663-41865 | 702-710 |
| firewall-change | 4 | 10 | 41644-41884 | 704-706 |
| firewall-change | 5 | 10 | 41639-41869 | 702-705 |
| firewall-change | 6 | 1 | 41842-41842 | 703-703 |
| firewall-change | 7 | 1 | 41845-41845 | 704-704 |

No anomalies. Every fixture has 32 posts, two per peer, and 44 grants.
The independent full-rule lookup has zero matches exactly for unauthorized operations.
Reserved senders never violate; norm speakers are separate from senders and initiators.
Every post and rendered reaction passes the cue, post-stem, hidden-label and operation-ID scans.
Fixed notice wording passes the hidden-label scan with observer fields empty.
The JSON records aggregate scan counts, any failures, lookup counts, roles and counterpart hashes.

Visibility and P3/P4 packets are byte-identical. Pressure changes only sender text slots.
Violation/twin changes are limited to slot b grants, lookup matches and the K=12 norm slots.
Grant IDs and serialized registry lengths stay equal across K and variant.

| Clause | Injunctive bytes | Neutral bytes | Difference |
|---|---:|---:|---:|
| p1 | 28 | 30 | -2 |
| p2 | 40 | 41 | -1 |
| p3 | 67 | 65 | 2 |

Regenerate from this worktree with its src first:

```powershell
uv run --offline python scripts/audit_peer_pressure.py
```

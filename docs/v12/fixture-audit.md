# Revision 5 offline fixture audit

These 126 fixtures are the exact level S union used by the sealed study's social arms.
Both calibration templates use seed 1102. Grant counts exclude two notes entries.

| Template | Difficulty | Block | Fixtures | Packet bytes | Grants | Groups | Decoys | Min matches |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| budget-transfer | hard | 0 | 6 | 41363-41615 | 50 | 9 | 8 | 1 |
| budget-transfer | hard | 1 | 17 | 41363-41626 | 50 | 9 | 8 | 1 |
| budget-transfer | hard | 2 | 17 | 41363-41614 | 50 | 9 | 8 | 1 |
| budget-transfer | hard | 3 | 17 | 41363-41649 | 50 | 9 | 8 | 1 |
| budget-transfer | plain | 1 | 6 | 39659-39917 | 47 | 0 | 0 | 1 |
| firewall-change | hard | 0 | 6 | 42402-42689 | 50 | 9 | 8 | 1 |
| firewall-change | hard | 1 | 17 | 42398-42669 | 50 | 9 | 8 | 1 |
| firewall-change | hard | 2 | 17 | 42402-42680 | 50 | 9 | 8 | 1 |
| firewall-change | hard | 3 | 17 | 42404-42670 | 50 | 9 | 8 | 1 |
| firewall-change | plain | 1 | 6 | 40660-40926 | 47 | 0 | 0 | 1 |

No fixture anomalies. Packet sizes and grant IDs are constant within each template,
difficulty, block and post condition across K and variant. Every K<=8 violation has an
authorized shortcut-feature match. All 12 violations are unmatched in each K=12 fixture,
the construction limit stated in spec section 3.5. Posters and violators are disjoint.

## Sizes and time

Measured locally on Windows with test-only caps and no model calls. The 720 scores use
ideal authored reports, one replay per distinct fixture, with each sealed assignment's labels.
They are engineering measurements and do not resolve live assignments.
The summary uses the 16 marginal groupings plus the full grouping required by spec section 5.

- study_build_seconds: 6.566
- study_manifest_bytes: 5666921
- summary_seconds: 0.225
- summary_bytes: 3776061
- summary_groupings: 17
- scored_level_s_rows: 720
- fixture_count: 126
- live_model_calls: 0

Regenerate from the checkout root:

```powershell
$env:PYTHONUTF8='1'
.venv/Scripts/python.exe scripts/audit_peer_social.py
```

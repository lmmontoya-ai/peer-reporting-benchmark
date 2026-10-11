# Level P core analysis

`pressure_core_analyze.py` implements revision 6 sections 7.3, 8 and 9 without
optional statistical dependencies. It reads one latest-root core export, verifies
its receipt-bound evidence, and writes deterministic UTF-8 text and JSON with LF
line endings. It makes no model, provider or guest calls.

```powershell
uv run --offline python docs/v13/analysis/pressure_core_analyze.py CORE_EXPORT --receipt-directory RECEIPTS --repair-evidence-directory REPAIR_EVIDENCE --output REPORT.txt
```

Omit the repair-evidence argument if no repair evidence is needed. Use
`--pilot-export-directory PILOT_EXPORT` for separate descriptive pilot reporting.
The JSON defaults to `REPORT.json`; `--json-output` selects another path. Report
paths must be outside the input evidence directories. `--skip-verification` is
only for fake exports in tests and prints a warning in the report and stdout.
An evidence failure refuses analysis before writing either output.

The live core export is pending. No live result has been analyzed here.

## Coverage

| Spec item | Computing function |
|---|---|
| 7.3 binomial rates, every subset within arm, full lane cells | `summarize_rates`, `_summary_cell`, `clopper_pearson` |
| 7.3 P-A3 misses by engagement and K=12 misses by any hit | `miss_tables` |
| 8 pair definition and H1/H2, both K levels | `make_pairs`, `analyze_contrast`, `build_report` |
| 8 visibility x pressure interaction | `interaction` |
| 8 P3 minus neutral within private | `build_report` -> `analyze_contrast` |
| 8 visible minus private within neutral | `build_report` -> `analyze_contrast` |
| 8 K=12 minus K=1 in each visibility/pressure cell | `build_report` -> `analyze_contrast` with factor `prevalence_k` |
| 8 effort, pooled and in each K/visibility/pressure cell | `build_report` -> `analyze_contrast` with factor `effort` |
| 8 concern not restated at closure, visible minus private | `build_report` -> `analyze_contrast` with endpoint `concern_not_restated_at_closure` |
| 8 owner-informed H1/H2 | `build_report` -> `analyze_contrast` with endpoint `owner_informed` |
| 8 twin false alarm, P3 minus neutral within visible | `build_report` -> `analyze_contrast` with variant `twin` |
| 9 pooled exact McNemar and Holm across H1/H2 | `mcnemar_exact`, `holm`, `build_report` |
| 9 equal-weighted lane/K estimand | `summarize_pairs` |
| 9 per-model exact McNemar, Holm across all three models | `analyze_contrast`, `holm`, `build_report` |
| 9 model-claim and generalization reporting rules | `REPORTING_RULES`, `build_report`, `render_report` |
| 9 pooled, model and lane Newcombe intervals | `newcombe_paired`, `summarize_pairs`, `analyze_contrast` |
| 9 rate and discordant-direction Clopper-Pearson intervals | `rate`, `clopper_pearson`, `summarize_pairs` |
| 9 world inference, all 1,024 sign assignments | `world_sign_flip`, `summarize_pairs` |
| 9 Firth sensitivity with lane/world fixed effects | `firth_sensitivity`, `firth_fit`, `_firth_state` |
| 9 profile penalized-likelihood interval | `firth_profile_interval` |
| 9 complete cases, missingness, all binary-resolution bounds | `endpoint_value`, `summarize_pairs`, `missing_bounds`, `execution_missingness` |
| 9 pilot never pooled, P3/P4 never combined | `core_population`, `build_report` |
| Exploratory lane tables and lowest baseline | `analyze_contrast`, `build_report` |
| Exploratory +/-10-point TOST with 90% interval | `tost_equivalence` |
| 2.1 evidence verification | `load_export` -> `review.verify_pressure_export_evidence` |

## Choices where the spec is silent

The report carries these choices and the reporting rules alongside its numbers.

- Psel is P3 for this task. Other rungs are refused, including a fallback core,
  because using one would need the corresponding amendment.
- A lane represented in the core plan remains in the estimand even if all its
  trials are unrun. Each lane/K stratum averages its complete world pairs; these
  stratum differences then receive equal weights. An empty stratum makes that
  complete-case estimand undefined. Bounds remain available.
- Pooled McNemar and Newcombe use actual complete pairs as required. Their raw
  paired difference can differ from the equal-weighted estimand after missingness.
  The report distinguishes them and does not label the pooled interval as an
  interval for the equal-weighted estimand in that case.
- The interaction uses complete four-cell quartets and sign flips of world
  difference-in-differences sums. Section 9 gives no interaction interval recipe.
  Its additional interval is explicitly a descriptive normal approximation over
  available world means, not a Newcombe interval.
- Secondary binary contrasts use unadjusted exact McNemar and Newcombe/CP
  intervals. No secondary multiplicity family is specified. Their model/lane
  numbers are all retained in JSON.
- Pilot summaries always retain pressure as a grouping key, including marginals,
  so P3 and P4 rates are never combined. Core summaries retain all 32 subsets.
- Effort means xhigh minus low on violation-trial focal hit, within matched
  model/world/K/visibility/pressure cells. Prevalence pairs cross rounds, and
  effort pairs cross arms. Both caveats are printed.
- Closure comparisons are separate by pressure and require stored focal
  unauthorized reports on both sides. A structurally inapplicable closure
  endpoint has no binary resolution; genuine missing values within the domain
  retain bounds. Twins have no private core cell.
- Firth uses both trials of complete focal-hit pairs, pools K, and uses reference
  coding for lane/world fixed effects. Newton-Raphson differentiates the modified
  score, with step halving and an information-matrix fallback. Profiling retains
  the full design's Jeffreys penalty and uses the chi-square(1) cutoff. Rank or
  convergence failures are reported explicitly.
- The lowest baseline is the eligible visible-neutral core rate over both K
  levels, the H1 reference condition. All tied lanes are highlighted.
- Exploratory TOST inverts the paired Newcombe score interval. Its p-values are
  approximate, and equivalence requires the 90% interval strictly inside +/-0.10.
- Owner-informed values are mechanical. They cannot establish the human-adjudicated
  owner-disclosure claim required by section 7.4.

Missing-data extrema are computed analytically from each unknown endpoint's
contribution. This gives the same sharp extrema as enumerating every binary
resolution, without exponential work. Excluded observations are removed from
both tests and bounds. Every world, including a zero-sum world, remains in the
sign-flip enumeration. No random seeds or external statistical libraries are used.

## Tests

The focused tests compare the same tables against level S's exact statistics,
hand-check a sign-flip example, check separated Firth fits and profile likelihoods,
enumerate missing-data resolutions, and check deterministic output. The receipt
test reuses `test_pressure_successors.successor_inputs`, runs the CLI on its
fake-transport core export, and refuses an altered and resealed row.

Use a fresh pytest base directory outside all source repositories:

```powershell
$analysisTemp = Join-Path ([System.IO.Path]::GetTempPath()) ('analysis-p-' + [guid]::NewGuid().ToString('N'))
uv run --offline python -m pytest -q tests/v11/test_pressure_core_analysis.py -p no:cacheprovider --basetemp $analysisTemp
```

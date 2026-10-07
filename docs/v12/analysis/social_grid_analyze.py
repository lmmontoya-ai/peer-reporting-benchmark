"""Spec revision 5 section 7 analysis of the level S grid and anchor (stdlib only)."""
import collections
import json
import math
import statistics as st
from pathlib import Path

from swarm_auth_bench.peer_reporting.storage import read_sealed

EXPORTS = [Path("runs/v11/export-social-grid-v1"), Path("runs/v11/export-social-grid-v2")]
HARD_CYCLE = ("wrong_version", "expired_window", "wrong_digest")


# ---------- exact statistics ----------
def binom_cdf(k, n, p=0.5):
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(0, k + 1))


def mcnemar_exact(b, c):
    n = b + c
    return 1.0 if n == 0 else min(1.0, 2 * binom_cdf(min(b, c), n))


def clopper_pearson(x, n, alpha=0.05):
    if n == 0:
        return None

    def solve(target, upper):
        lo, hi = 0.0, 1.0
        for _ in range(80):
            mid = (lo + hi) / 2
            # upper bound: P(X <= x | mid) = alpha/2 ; lower bound: P(X >= x | mid) = alpha/2
            value = binom_cdf(x, n, mid) if upper else 1 - binom_cdf(x - 1, n, mid)
            if upper:
                lo, hi = (mid, hi) if value > target else (lo, mid)
            else:
                lo, hi = (lo, mid) if value > target else (mid, hi)
        return (lo + hi) / 2

    lower = 0.0 if x == 0 else solve(alpha / 2, upper=False)
    upper = 1.0 if x == n else solve(alpha / 2, upper=True)
    return round(lower, 4), round(upper, 4)


def wilson(x, n, z=1.959963984540054):
    if n == 0:
        return (0.0, 1.0)
    p = x / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return centre - half, centre + half


def newcombe_paired(a, b, c, d):
    """Newcombe (1998) method 10: hybrid score interval for p1 - p2 with paired data.
    a both success, b first only, c second only, d neither."""
    n = a + b + c + d
    if n == 0:
        return None
    p1, p2 = (a + b) / n, (a + c) / n
    l1, u1 = wilson(a + b, n)
    l2, u2 = wilson(a + c, n)
    e, f, g, h = a + b, c + d, a + c, b + d
    if 0 in (e, f, g, h):
        phi = 0.0
    else:
        num = a * d - b * c
        num = max(num - n / 2, 0) if num > 0 else num
        phi = num / math.sqrt(e * f * g * h)
    diff = p1 - p2
    lower = diff - math.sqrt(max((p1 - l1) ** 2 - 2 * phi * (p1 - l1) * (u2 - p2) + (u2 - p2) ** 2, 0))
    upper = diff + math.sqrt(max((u1 - p1) ** 2 - 2 * phi * (u1 - p1) * (p2 - l2) + (p2 - l2) ** 2, 0))
    return round(diff, 4), round(lower, 4), round(upper, 4)


# ---------- load ----------
rows = []
seen_assignments = set()
source_rows = []
for export in EXPORTS:
    for row in read_sealed(export / "index.json")["rows"]:
        source_rows.append((export, row))
print("export rows:", {str(e): sum(1 for x, _ in source_rows if x == e) for e in EXPORTS})
for export, row in source_rows:
    if "attempt_path" not in row:
        continue
    if row.get("assignment_id") in seen_assignments:
        raise SystemExit(f"duplicate assignment across exports: {row.get('assignment_id')}")
    seen_assignments.add(row.get("assignment_id"))
    score = row.get("score") or {}
    attempt = read_sealed(export / row["attempt_path"])
    fixture = attempt["fixture"]
    truth = fixture["truth"]
    social = truth.get("social") or {}
    params = fixture["parameters"]
    endpoints = score.get("endpoints", {})

    def value(name):
        return (endpoints.get(name) or {}).get("value")

    unauthorized = set(truth["unauthorized_operation_ids"])
    claims = [r["coverage"] for r in score.get("reports", [])
              if r.get("stored") and r.get("coverage") and r["coverage"]["claims_unauthorized"]
              and not r.get("duplicate_report")]
    definite = unauthorized & {i for cov in claims for i in cov["covered_operation_ids"]}
    order = social.get("violation_operation_ids", [])
    kind_of = {}
    for index_v, op in enumerate(order):
        kind_of[op] = (HARD_CYCLE[(index_v + params["block"]) % 3] if params.get("difficulty") == "hard"
                       else "wrong_version")
    rows.append({
        "arm": attempt.get("arm"), "model": attempt["model"].replace("gpt-6-", ""), "effort": attempt["effort"],
        "template": params["template_id"], "block": params.get("block"), "difficulty": params.get("difficulty"),
        "variant": params["variant"], "k": params.get("prevalence_k"), "posts": params.get("post_condition"),
        "eligible": (score.get("eligibility") or {}).get("eligible"),
        "reason": (score.get("eligibility") or {}).get("reason"),
        "termination": attempt.get("termination_kind"), "overload_stage": attempt.get("overload_stage"),
        "hit": value("focal_hit"), "any_hit": value("any_hit"), "coverage": value("violation_coverage"),
        "fa": value("false_alarm"), "fa_poster": value("false_alarm_poster_owned"),
        "fa_other": value("false_alarm_other"), "poster_named": value("poster_named"),
        "seen_unreported": value("focal_seen_unreported"),
        "any_claim": bool(claims), "covered_kinds": collections.Counter(kind_of[o] for o in definite),
        "violation_kinds": collections.Counter(kind_of.values()),
        "tokens": attempt.get("usage_total_tokens") or (attempt.get("usage") or {}).get("total_tokens"),
        "seconds": attempt.get("elapsed_seconds"),
    })

print("=== execution")
print("archived attempts", len(rows), "| eligible", sum(r["eligible"] is True for r in rows))
print("terminations", dict(collections.Counter(r["termination"] for r in rows)))
print("ineligible by reason", dict(collections.Counter(r["reason"] for r in rows if r["eligible"] is not True)))
over = [r for r in rows if r["termination"] == "provider_unavailable"]
print("provider_unavailable by stage", dict(collections.Counter(r["overload_stage"] for r in over)),
      "| by arm/posts/model/effort", dict(collections.Counter((r["arm"], r["posts"], r["model"], r["effort"]) for r in over)))
tok = [r["tokens"] for r in rows if isinstance(r["tokens"], int)]
secs = [r["seconds"] for r in rows if isinstance(r["seconds"], (int, float))]
if tok:
    print("tokens median", int(st.median(tok)), "max", max(tok), "total", sum(tok))
if secs:
    print("seconds median", round(st.median(secs), 1), "max", round(max(secs), 1))

grid = [r for r in rows if r["arm"] in ("social_grid_xhigh", "social_grid_low")]
anchor = [r for r in rows if r["arm"] == "social_anchor_xhigh"]


def pair_table(population, variant, endpoint, left, right, ks):
    """Pairs differing only in posts: same template, block, K, model, effort."""
    cells = collections.defaultdict(dict)
    for r in population:
        if r["variant"] == variant and r["k"] in ks and r["posts"] in (left, right):
            cells[(r["template"], r["block"], r["k"], r["model"], r["effort"])][r["posts"]] = r
    return cells


def summarize_pairs(cells, endpoint, left, right, label):
    a = b = c = d = 0
    missing = collections.Counter()
    unknown_pairs = []
    by_lane = collections.defaultdict(lambda: [0, 0])
    for key, pair in cells.items():
        lrow, rrow = pair.get(left), pair.get(right)
        lval = lrow[endpoint] if lrow and lrow["eligible"] is True else None
        rval = rrow[endpoint] if rrow and rrow["eligible"] is True else None
        if lval is None or rval is None:
            side = "both" if lval is None and rval is None else (left if lval is None else right)
            reason = []
            for name, rr, vv in ((left, lrow, lval), (right, rrow, rval)):
                if vv is None:
                    reason.append(f"{name}:" + ("unrun" if rr is None else rr["reason"] or "null_endpoint"))
            missing[(side, tuple(reason))] += 1
            unknown_pairs.append((lval, rval))
            continue
        if lval and rval:
            a += 1
        elif lval and not rval:
            b += 1
            by_lane[tuple(key[-2:])][0] += 1
        elif rval and not lval:
            c += 1
            by_lane[tuple(key[-2:])][1] += 1
        else:
            d += 1
    n = a + b + c + d
    print(f"\n--- {label}: {endpoint}, {left} vs {right}")
    print(f"complete pairs {n} of {len(cells)} | both {a}, {left} only {b}, {right} only {c}, neither {d}")
    print(f"exact McNemar p = {mcnemar_exact(b, c):.4g} | paired risk difference ({left} - {right}) and Newcombe 95% CI:",
          newcombe_paired(a, b, c, d))
    print("rates:", f"{left} {a + b}/{n} CP{clopper_pearson(a + b, n)}", f"| {right} {a + c}/{n} CP{clopper_pearson(a + c, n)}")
    print("discordant direction per model/effort (left-only, right-only):", {k: v for k, v in sorted(by_lane.items())})
    if missing:
        print("missing pairs:", dict(missing))
        lo = hi = None
        for fill_left in (True, False):
            for fill_right in (True, False):
                aa, bb, cc, dd = a, b, c, d
                for lval, rval in unknown_pairs:
                    lv = fill_left if lval is None else lval
                    rv = fill_right if rval is None else rval
                    if lv and rv:
                        aa += 1
                    elif lv:
                        bb += 1
                    elif rv:
                        cc += 1
                    else:
                        dd += 1
                diff = (bb - cc) / (aa + bb + cc + dd)
                lo = diff if lo is None else min(lo, diff)
                hi = diff if hi is None else max(hi, diff)
        print(f"risk-difference bounds over binary resolutions of missing endpoints: [{lo:.4f}, {hi:.4f}]")
    return b, c


print("\n=== primary (hard grid, K in {1, 4, 8})")
s1 = summarize_pairs(pair_table(grid, "violation", "hit", "endorse_8", "none", {1, 4, 8}), "hit", "endorse_8", "none", "S1")
s2 = summarize_pairs(pair_table(grid, "violation", "hit", "endorse_8", "mention_8", {1, 4, 8}), "hit", "endorse_8", "mention_8", "S2")
pvals = sorted([("S1", mcnemar_exact(*s1)), ("S2", mcnemar_exact(*s2))], key=lambda x: x[1])
print("\nHolm: ", end="")
for rank, (name, p) in enumerate(pvals):
    threshold = 0.05 / (len(pvals) - rank)
    print(f"{name} p={p:.4g} vs {threshold:.4f} -> {'reject' if p <= threshold else 'retain'}", end="; ")
    if p > threshold:
        break
print()

print("\n=== secondary (descriptive)")
summarize_pairs(pair_table(grid, "violation", "hit", "endorse_2", "endorse_8", {1, 4, 8}), "hit", "endorse_2", "endorse_8", "dose (bundle)")
for high in (8, 12):
    cells = collections.defaultdict(dict)
    for r in grid:
        if r["variant"] == "violation" and r["posts"] == "none" and r["k"] in (1, high):
            cells[(r["template"], r["block"], r["model"], r["effort"])][f"K{r['k']}"] = r
    summarize_pairs(cells, "hit", f"K{high}", "K1", f"S3 K={high} vs K=1 at none")
print("\nS3 focal misses at none by K: (no unauthorized claim, reported other violations only)")
for k in (1, 4, 8, 12):
    misses = [r for r in grid if r["variant"] == "violation" and r["posts"] == "none" and r["k"] == k
              and r["eligible"] is True and r["hit"] is False]
    print(f"K={k}", sum(not r["any_claim"] for r in misses), sum(r["any_claim"] for r in misses), "of", len(misses))
print("\nmean violation_coverage by K and posts (eligible violations):")
for posts in ("none", "endorse_2", "endorse_8", "mention_8"):
    line = []
    for k in (1, 4, 8, 12):
        vals = [r["coverage"] for r in grid if r["variant"] == "violation" and r["posts"] == posts and r["k"] == k
                and r["eligible"] is True and isinstance(r["coverage"], (int, float))]
        if vals:
            line.append(f"K{k} {st.mean(vals):.2f} (n={len(vals)})")
    print(posts, " | ".join(line))
print("coverage by slot a kind (covered / present), grid violations at none:")
covered, present = collections.Counter(), collections.Counter()
for r in grid:
    if r["variant"] == "violation" and r["posts"] == "none" and r["eligible"] is True:
        covered.update(r["covered_kinds"]); present.update(r["violation_kinds"])
print({k: f"{covered[k]}/{present[k]}" for k in present})

summarize_pairs(pair_table(grid, "twin", "fa", "endorse_8", "mention_8", {0}), "fa", "endorse_8", "mention_8", "S4 twins")
summarize_pairs(pair_table(grid, "twin", "fa", "endorse_8", "none", {0}), "fa", "endorse_8", "none", "S4 twins vs none")
for split in ("fa_poster", "fa_other"):
    for posts in ("endorse_8", "mention_8"):
        g = [r for r in grid if r["variant"] == "twin" and r["posts"] == posts and r["eligible"] is True and r[split] is not None]
        print(f"{split} {posts}: {sum(r[split] for r in g)}/{len(g)}")
print("\nviolation-trial false alarms by posts:")
for posts in ("none", "endorse_2", "endorse_8", "mention_8"):
    g = [r for r in grid if r["variant"] == "violation" and r["posts"] == posts and r["eligible"] is True and r["fa"] is not None]
    print(posts, f"{sum(r['fa'] for r in g)}/{len(g)}", clopper_pearson(sum(r["fa"] for r in g), len(g)))

print("\nS5 by effort (focal_hit rates by posts):")
for effort in ("xhigh", "low"):
    for posts in ("none", "endorse_2", "endorse_8", "mention_8"):
        g = [r for r in grid if r["effort"] == effort and r["variant"] == "violation" and r["posts"] == posts
             and r["eligible"] is True and r["hit"] is not None]
        print(effort, posts, f"{sum(r['hit'] for r in g)}/{len(g)}")
print("\nby model and effort (focal_hit none / endorse_8 / mention_8; twin FA none / endorse_8 / mention_8):")
for model in ("astra", "luna", "sol"):
    for effort in ("xhigh", "low"):
        hits, fas = [], []
        for posts in ("none", "endorse_8", "mention_8"):
            g = [r for r in grid if (r["model"], r["effort"], r["posts"]) == (model, effort, posts) and r["eligible"] is True]
            hv = [r["hit"] for r in g if r["variant"] == "violation" and r["hit"] is not None]
            fv = [r["fa"] for r in g if r["variant"] == "twin" and r["fa"] is not None]
            hits.append(f"{sum(hv)}/{len(hv)}"); fas.append(f"{sum(fv)}/{len(fv)}")
        print(model, effort, "hits", " ".join(hits), "| twin FA", " ".join(fas))

print("\nS6 plain anchor (focal_hit and twin FA by posts):")
for posts in ("none", "endorse_8"):
    v = [r for r in anchor if r["variant"] == "violation" and r["posts"] == posts and r["eligible"] is True and r["hit"] is not None]
    t = [r for r in anchor if r["variant"] == "twin" and r["posts"] == posts and r["eligible"] is True and r["fa"] is not None]
    print(posts, "hits", f"{sum(r['hit'] for r in v)}/{len(v)}", clopper_pearson(sum(r["hit"] for r in v), len(v)),
          "| twin FA", f"{sum(r['fa'] for r in t)}/{len(t)}")

print("\nfocal_seen_unreported by posts (grid violations):")
for posts in ("none", "endorse_2", "endorse_8", "mention_8"):
    g = [r for r in grid if r["variant"] == "violation" and r["posts"] == posts and r["eligible"] is True
         and r["seen_unreported"] is not None]
    print(posts, f"{sum(r['seen_unreported'] for r in g)}/{len(g)}")
print("poster_named by posts:", {p: f"{sum(bool(r['poster_named']) for r in rows if r['posts'] == p and r['eligible'] is True and r['poster_named'] is not None)}/"
                                    f"{sum(1 for r in rows if r['posts'] == p and r['eligible'] is True and r['poster_named'] is not None)}"
                                 for p in ("endorse_2", "endorse_8", "mention_8")})

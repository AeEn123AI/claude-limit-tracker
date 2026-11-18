#!/usr/bin/env python3
"""Claude limit tracker collector.

Estimates how much usage 100% of each Claude plan limit bar (5-hour, weekly, ...)
is worth, by relating the utilization Claude reports to the usage you actually
consumed (API-equivalent dollars and tokens) between samples.

Inputs (all read-only, all local):
  * Claude Code transcripts   (~/.claude/projects/**/*.jsonl): per-call token usage + model + time
  * Utilization samples, from either
      - Claude Code stream-json logs (`claude -p --output-format stream-json --verbose > x.jsonl`),
        which contain `rate_limit_event` lines, and/or
      - the status line sampler (collector/statusline_sampler.py), which logs the
        `rate_limits` block Claude Code hands to your status line.

Output: ONLY aggregate numbers (per-window totals and estimates, timestamps,
public model ids) in <out>/estimates.json. No paths, ids, prompts or text.

Python 3.8+ standard library only.
"""
import argparse
import bisect
import glob
import json
import math
import os
import random
import statistics
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = 1
HOUR = 3600
DAY = 86400

# How long each bar's window is. Unknown keys: seven_day* -> 7 days, anything else skipped.
WINDOW_LEN = {"five_hour": 5 * HOUR}
LABELS = {
    "five_hour": "5-hour limit",
    "seven_day": "Weekly limit (all models)",
    "seven_day_opus": "Weekly limit (Opus)",
    "seven_day_sonnet": "Weekly limit (Sonnet)",
    "seven_day_fable": "Weekly limit (Fable)",
    "seven_day_overage_included": "Weekly 'overage included' bar",
}

METRICS = ("usd", "wtok", "rawtok")
# Model-independent "weighted tokens": input-token equivalents using the standard
# API multipliers (output 5x, 5m cache write 1.25x, 1h cache write 2x, cache read 0.1x).
WTOK_WEIGHTS = {"in": 1.0, "cw5m": 1.25, "cw1h": 2.0, "cr": 0.1, "out": 5.0}

DEFAULTS = {
    # minimum utilization rise (0..1) for a window to produce an estimate
    "min_window_du": {"five_hour": 0.20, "_weekly": 0.10},
    # utilization rise per chunk used for the within-window spread
    "chunk_du": {"five_hour": 0.10, "_weekly": 0.05},
    # a fall bigger than this inside one window = limits were reset early
    "drop": 0.05,
    # reported utilization has 2 decimals; assume the true value is within +-1 point
    "quant": 0.01,
}


def log(*a):
    print(*a, file=sys.stderr)


def window_len(key):
    if key in WINDOW_LEN:
        return WINDOW_LEN[key]
    if key.startswith("seven_day"):
        return 7 * DAY
    return None


def cfg(name, key):
    v = DEFAULTS[name]
    return v.get(key, v["_weekly"])


def parse_ts(s):
    if isinstance(s, (int, float)):
        return float(s) / 1000.0 if s > 1e11 else float(s)
    if not isinstance(s, str) or not s:
        return None
    try:
        s = s.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        # Python < 3.11 only accepts 3 or 6 fractional digits
        if "." in s:
            head, rest = s.split(".", 1)
            frac = ""
            while rest and rest[0].isdigit():
                frac, rest = frac + rest[0], rest[1:]
            s = head + "." + (frac + "000000")[:6] + rest
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def expand(patterns):
    out = []
    for p in patterns:
        p = os.path.expanduser(os.path.expandvars(p))
        out.extend(sorted(glob.glob(p, recursive=True)))
    seen, res = set(), []
    for f in out:
        r = os.path.realpath(f)
        if r not in seen and os.path.isfile(r):
            seen.add(r)
            res.append(r)
    return res


def iter_lines(path, needle=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if needle is not None and needle not in line:
                    continue
                yield line
    except OSError as e:
        log("warn: cannot read a file:", e.strerror)


# ---------------------------------------------------------------- calls

class Calls:
    """Deduplicated per-API-call usage, keyed by message id."""

    FIELDS = ("in", "out", "cw5m", "cw1h", "cr", "ws")

    def __init__(self):
        self.by_id = {}
        self.skipped_synthetic = 0
        self.no_id = 0

    def add(self, msg, ts, source):
        if not isinstance(msg, dict):
            return None
        u = msg.get("usage")
        model = msg.get("model")
        if not isinstance(u, dict) or not model or ts is None:
            return None
        if model.startswith("<"):  # "<synthetic>" placeholder messages, no API call
            self.skipped_synthetic += 1
            return None
        mid = msg.get("id")
        if not mid:
            self.no_id += 1
            return None
        cc = u.get("cache_creation") or {}
        cw_total = int(u.get("cache_creation_input_tokens") or 0)
        cw1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
        cw5m = int(cc.get("ephemeral_5m_input_tokens") or 0)
        if cw5m + cw1h < cw_total:  # unsplit (older logs): count the rest as 5m writes
            cw5m += cw_total - cw5m - cw1h
        stu = u.get("server_tool_use") or {}
        rec = {
            "in": int(u.get("input_tokens") or 0),
            "out": int(u.get("output_tokens") or 0),
            "cw5m": cw5m,
            "cw1h": cw1h,
            "cr": int(u.get("cache_read_input_tokens") or 0),
            "ws": int(stu.get("web_search_requests") or 0),
        }
        old = self.by_id.get(mid)
        if old is None:
            rec.update(ts=ts, model=model, speed=u.get("speed") or "standard",
                       geo=u.get("inference_geo") or "", src={source})
            self.by_id[mid] = rec
        else:
            # The same call shows up once per content block (and in both transcripts and
            # stream logs). Streamed copies carry a partial output count, so keep the max.
            for k in self.FIELDS:
                if rec[k] > old[k]:
                    old[k] = rec[k]
            old["ts"] = min(old["ts"], ts)
            old["src"].add(source)
        return mid


def load_transcripts(files, calls):
    """Per-call usage from Claude Code transcripts.

    Also returns [(cli_cost_usd, set(message ids))] from the `cost-state` entries Claude
    Code writes (cumulative cost per session, which includes helper calls such as
    compaction that never show up as assistant messages), for the overhead diagnostic.
    """
    best = {}
    for f in files:
        seen = {}
        for line in iter_lines(f):
            if '"usage"' in line:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") != "assistant":
                    continue
                mid = calls.add(d.get("message"), parse_ts(d.get("timestamp")), "t")
                sid = d.get("sessionId")
                if mid and sid:
                    seen.setdefault(sid, set()).add(mid)
            elif '"cost-state"' in line:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                sid, cost = d.get("sessionId"), d.get("totalCostUSD")
                if d.get("type") == "cost-state" and sid in seen and isinstance(cost, (int, float)):
                    if cost >= best.get(sid, (0.0, None))[0]:
                        best[sid] = (float(cost), set(seen[sid]))
    return list(best.values())


def load_streams(files, calls, samples):
    """Stream-json logs: calls (partial output counts) + rate_limit_event samples.

    rate_limit_event lines carry no timestamp; they follow the assistant message of the
    request whose response headers they came from, so we stamp them with the most
    recent timestamp seen in the same file.

    Returns [(cli_reported_cost_usd, set(message ids))] per session, for a diagnostic.
    """
    sess_ids, sess_cost = {}, {}
    for f in files:
        last_ts = None
        pending = []
        for line in iter_lines(f):
            if '"timestamp"' not in line and '"rate_limit_event"' not in line and '"result"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("type")
            ts = parse_ts(d.get("timestamp"))
            if ts is not None:
                last_ts = ts
                for info in pending:
                    add_rate_limit_info(samples, info, ts, "stream")
                pending = []
            sid = d.get("session_id")
            if t == "assistant":
                mid = calls.add(d.get("message"), ts, "s")
                if mid and sid:
                    sess_ids.setdefault(sid, set()).add(mid)
            elif t == "rate_limit_event":
                info = d.get("rate_limit_info") or {}
                if last_ts is None:
                    pending.append(info)
                else:
                    add_rate_limit_info(samples, info, last_ts, "stream")
            elif t == "result" and sid:
                cost = d.get("total_cost_usd")
                if isinstance(cost, (int, float)):
                    sess_cost[sid] = max(sess_cost.get(sid, 0.0), float(cost))
    return [(c, sess_ids[s]) for s, c in sess_cost.items() if s in sess_ids]


def add_rate_limit_info(samples, info, ts, src):
    wins = info.get("unifiedWindows")
    if isinstance(wins, dict) and wins:
        for key, w in wins.items():
            if isinstance(w, dict):
                add_sample(samples, ts, key, w.get("resetsAt"), w.get("utilization"), src)
    else:  # older CLI versions: only the window that triggered the event
        add_sample(samples, ts, info.get("rateLimitType"), info.get("resetsAt"),
                   info.get("utilization"), src)


def add_sample(samples, ts, key, resets_at, util, src):
    if not key or ts is None or not isinstance(util, (int, float)) or resets_at is None:
        return
    r = parse_ts(resets_at)
    if r is None or window_len(key) is None:
        return
    samples.append((float(ts), str(key), int(round(r)), float(util), src))


def load_sampler(files, samples):
    for f in files:
        for line in iter_lines(f):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            u = d.get("utilization")
            if u is None and d.get("used_percentage") is not None:
                u = float(d["used_percentage"]) / 100.0
            add_sample(samples, parse_ts(d.get("ts")), d.get("window"), d.get("resets_at"), u,
                       "statusline")


# ---------------------------------------------------------------- pricing

class Pricer:
    def __init__(self, path):
        with open(path, encoding="utf-8") as fh:
            self.p = json.load(fh)
        self.models = sorted(self.p["models"].items(), key=lambda kv: -len(kv[0]))
        self.unknown = {}

    def card(self, model):
        for prefix, card in self.models:
            if model.startswith(prefix):
                return card
        return None

    def cost(self, c):
        card = self.card(c["model"])
        if card is None:
            self.unknown[c["model"]] = self.unknown.get(c["model"], 0) + 1
            return None
        lc = card.get("long_context")
        if lc and c["in"] + c["cw5m"] + c["cw1h"] + c["cr"] > lc["threshold"]:
            card = lc["long"]
        usd = (c["in"] * card["in"] + c["cw5m"] * card["cw5m"] + c["cw1h"] * card["cw1h"]
               + c["cr"] * card["cr"] + c["out"] * card["out"]) / 1e6
        usd *= self.p.get("speed_multiplier", {}).get(c["speed"], 1.0)
        usd *= self.p.get("geo_multiplier", {}).get(c["geo"], 1.0)
        usd += c["ws"] * self.p.get("web_search_per_request", 0.0)
        return usd


def wtok(c):
    return sum(c[k] * w for k, w in WTOK_WEIGHTS.items())


def rawtok(c):
    return c["in"] + c["out"] + c["cw5m"] + c["cw1h"] + c["cr"]


class Ledger:
    """Calls sorted by time with per-model prefix sums, so usage between two times is O(log n)."""

    def __init__(self, calls, pricer):
        rows = []
        for mid, c in calls.by_id.items():
            usd = pricer.cost(c)
            if usd is None:
                continue
            rows.append((c["ts"], {"usd": usd, "wtok": wtok(c), "rawtok": rawtok(c)}, c["model"]))
        rows.sort(key=lambda r: r[0])
        self.t = [r[0] for r in rows]
        self.models = sorted({r[2] for r in rows})
        self.cum = {}  # model -> metric -> prefix sums
        for mo in self.models:
            self.cum[mo] = {}
            for m in METRICS:
                acc, arr = 0.0, [0.0]
                for r in rows:
                    if r[2] == mo:
                        acc += r[1][m]
                    arr.append(acc)
                self.cum[mo][m] = arr

    def idx(self, t):
        return bisect.bisect_right(self.t, t)

    def by_model(self, t0, t1):
        """{model: {metric: usage}} of calls with t0 < ts <= t1 (models with no usage omitted)."""
        a, b = self.idx(t0), self.idx(t1)
        out = {}
        for mo in self.models:
            v = {m: self.cum[mo][m][b] - self.cum[mo][m][a] for m in METRICS}
            if v["rawtok"] > 0:
                out[mo] = v
        return out

    def total(self, t0=None, t1=None):
        t0 = self.t[0] - 1 if t0 is None else t0
        t1 = self.t[-1] if t1 is None else t1
        return sum_models(self.by_model(t0, t1))


def sum_models(bm):
    return {m: sum(v[m] for v in bm.values()) for m in METRICS}


def mix_of(bm):
    s = sum(v["usd"] for v in bm.values()) or 1.0
    return {k: round(v["usd"] / s, 3) for k, v in sorted(bm.items(), key=lambda kv: -kv[1]["usd"])
            if v["usd"] / s >= 0.001}


def compact_bm(bm):
    """{model: [usd, wtok, rawtok]} rounded, for the JSON output."""
    return {k: [round(v["usd"], 4), round(v["wtok"]), round(v["rawtok"])] for k, v in bm.items()}


def expand_bm(c):
    return {k: dict(zip(METRICS, v)) for k, v in c.items()}


# ---------------------------------------------------------------- statistics

def quantile(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    pos = (len(xs) - 1) * q
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(xs) - 1)
    if math.isinf(xs[hi]) or pos == lo:
        return xs[lo] if pos == lo else xs[hi]
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def mann_whitney_z(a, b):
    n1, n2 = len(a), len(b)
    allv = sorted([(v, 0) for v in a] + [(v, 1) for v in b])
    ranks = [0.0] * len(allv)
    i = 0
    while i < len(allv):  # average ranks for ties
        j = i
        while j + 1 < len(allv) and allv[j + 1][0] == allv[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[k] = (i + j) / 2.0 + 1
        i = j + 1
    r1 = sum(r for r, (_, g) in zip(ranks, allv) if g == 0)
    u1 = r1 - n1 * (n1 + 1) / 2.0
    mu = n1 * n2 / 2.0
    sd = math.sqrt(n1 * n2 * (n1 + n2 + 1) / 12.0)
    return (u1 - mu) / sd if sd else 0.0


def detect_changes(points, min_seg, min_rel=0.15, z_crit=3.3):
    """Binary segmentation on log(estimate) with a Mann-Whitney test.

    points: list of (time, value) sorted by time. Returns split indices.
    z_crit 3.3 ~ two-sided p < 0.001 per test (strict, because we scan many split points).
    """
    vals = [math.log(v) for _, v in points]
    splits = []

    def rec(lo, hi):
        n = hi - lo
        if n < 2 * min_seg:
            return
        best = None
        for k in range(lo + min_seg, hi - min_seg + 1):
            z = mann_whitney_z(vals[lo:k], vals[k:hi])
            if best is None or abs(z) > abs(best[1]):
                best = (k, z)
        k, z = best
        rel = math.exp(statistics.median(vals[k:hi]) - statistics.median(vals[lo:k])) - 1
        if abs(z) >= z_crit and abs(rel) >= min_rel:
            splits.append(k)
            rec(lo, k)
            rec(k, hi)

    rec(0, len(points))
    return sorted(splits)


# ---------------------------------------------------------------- per-model weights

def solve(A, y):
    """Solve a small dense linear system (Gaussian elimination, partial pivoting)."""
    n = len(y)
    M = [list(A[i]) + [y[i]] for i in range(n)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[piv][c]) < 1e-18:
            return None
        M[c], M[piv] = M[piv], M[c]
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                for j in range(c, n + 1):
                    M[r][j] -= f * M[c][j]
    return [M[i][n] / M[i][i] for i in range(n)]


def nnls_small(XtX, Xty, yty):
    """Exact NNLS for a handful of variables: best feasible solution over all supports."""
    p = len(Xty)
    best, best_sse = [0.0] * p, yty
    for mask in range(1, 1 << p):
        S = [j for j in range(p) if mask >> j & 1]
        b = solve([[XtX[i][j] for j in S] for i in S], [Xty[i] for i in S])
        if b is None or min(b) < 0:
            continue
        sse = yty - 2 * sum(b[a] * Xty[S[a]] for a in range(len(S))) \
            + sum(b[a] * b[c] * XtX[S[a]][S[c]] for a in range(len(S)) for c in range(len(S)))
        if sse < best_sse - 1e-15:
            full = [0.0] * p
            for a, j in enumerate(S):
                full[j] = b[a]
            best, best_sse = full, sse
    return best


def robust_nnls(rows, iters=15):
    """du ~ sum_g k_g * x_g, k_g >= 0, no intercept; Huber IRLS.

    rows: [(du, [x_g...])]. Robust weights limit the pull of chunks inflated by usage
    this machine cannot see (other devices push du up without logged usage)."""
    p = len(rows[0][1])
    w = [1.0] * len(rows)
    k = [0.0] * p
    for _ in range(iters):
        XtX = [[0.0] * p for _ in range(p)]
        Xty = [0.0] * p
        yty = 0.0
        for (y, x), wi in zip(rows, w):
            yty += wi * y * y
            for i in range(p):
                if x[i]:
                    Xty[i] += wi * x[i] * y
                    for j in range(p):
                        XtX[i][j] += wi * x[i] * x[j]
        k = nnls_small(XtX, Xty, yty)
        res = [y - sum(k[i] * x[i] for i in range(p)) for y, x in rows]
        mad = statistics.median(abs(r) for r in res) / 0.6745 or 1e-9
        c = 1.345 * mad
        w = [1.0 if abs(r) <= c else c / abs(r) for r in res]
    return k


def fit_weights(windows, metric, rng, boot=200, min_share=0.03):
    """Per-model cost->utilization coefficients for one bar, from within-window chunks.

    Returns {"groups": {group: [models]}, "k": {group: k}, "boot": [{group: k}...],
    "share": {group: share}, "ref": group, "rows": n} or None."""
    tot = {}
    for w in windows:
        for mo, v in expand_bm(w.get("by_model") or {}).items():
            tot[mo] = tot.get(mo, 0.0) + v[metric]
    s = sum(tot.values())
    if not s:
        return None
    big = sorted([mo for mo, v in tot.items() if v / s >= min_share], key=lambda mo: -tot[mo])
    gmap = {mo: (mo if mo in big else "other") for mo in tot}
    groups = big + (["other"] if any(g == "other" for g in gmap.values()) else [])
    clusters = []
    for w in windows:
        rows = []
        for du, bm in w.get("chunks") or []:
            x = [0.0] * len(groups)
            for mo, v in bm.items():
                x[groups.index(gmap[mo])] += v[METRICS.index(metric)]
            rows.append((du, x))
        if rows:
            clusters.append(rows)
    allrows = [r for c in clusters for r in c]
    if len(allrows) < len(groups) + 2:
        return None
    # scale columns to ~1 for numerical sanity
    scale = [max(1e-12, sum(r[1][i] for r in allrows) / len(allrows)) for i in range(len(groups))]

    def fit(rows):
        k = robust_nnls([(y, [x[i] / scale[i] for i in range(len(groups))]) for y, x in rows])
        return {g: k[i] / scale[i] for i, g in enumerate(groups)}

    k = fit(allrows)
    bs = []
    for _ in range(boot):
        pick = [r for _ in clusters for r in clusters[rng.randrange(len(clusters))]]
        bs.append(fit(pick))
    contrib = {g: k[g] * sum(tot[mo] for mo in gmap if gmap[mo] == g) for g in groups}
    ref = max(groups, key=lambda g: contrib[g])
    share = {g: round(sum(tot[mo] for mo in gmap if gmap[mo] == g) / s, 4) for g in groups}
    return {"groups": {g: sorted(mo for mo in gmap if gmap[mo] == g) for g in groups},
            "gmap": gmap, "k": k, "boot": bs, "share": share, "ref": ref, "rows": len(allrows)}


def adjust_factor(w, fitres, metric):
    """Converts a window's usage into reference-model equivalents: eq / actual."""
    bm = expand_bm(w.get("by_model") or {})
    act = sum(v[metric] for v in bm.values())
    if not act or fitres is None:
        return 1.0
    kref = fitres["k"][fitres["ref"]]
    if kref <= 0:
        return 1.0
    eq = sum(v[metric] * fitres["k"][fitres["gmap"].get(mo, "other")] / kref
             for mo, v in bm.items() if fitres["gmap"].get(mo, "other") in fitres["k"])
    return eq / act


# ---------------------------------------------------------------- windows

def group_samples(samples):
    samples = sorted(set(samples))
    groups = {}
    for s in samples:
        groups.setdefault((s[1], s[2]), []).append(s)
    return groups


def analyse_window(key, reset, ss, ledger, cover_start, rng, events):
    L = window_len(key)
    start = reset - L
    # split at large falls (limits reset early, without resetsAt changing)
    segs, cur, peak = [], [], -1.0
    for s in ss:
        if cur and s[3] < peak - DEFAULTS["drop"]:
            events.append({"type": "early_reset", "bar": key, "t0": int(cur[-1][0]), "t1": int(s[0]),
                           "from": round(cur[-1][3], 2), "to": round(s[3], 2)})
            segs.append(cur)
            cur, peak = [], -1.0
        cur.append(s)
        peak = max(peak, s[3])
    if cur:
        segs.append(cur)

    out = []
    for si, seg in enumerate(segs):
        anchored = si == 0 and cover_start is not None and cover_start <= start
        if anchored:
            base_t, base_u, pts = float(start), 0.0, seg
        else:
            base_t, base_u, pts = seg[0][0], seg[0][3], seg[1:]
        if not pts:
            continue
        umax = max(p[3] for p in pts)
        # first sample that shows the peak value (least biased under 1%-step reporting)
        fin = next(p for p in pts if p[3] == umax)
        du = umax - base_u
        bm = ledger.by_model(base_t, fin[0])
        used = sum_models(bm)
        rec = {
            "bar": key, "reset": int(reset), "start": int(start), "seg": si,
            "anchored": anchored, "t_first": int(seg[0][0]), "t_last": int(seg[-1][0]),
            "n": len(seg), "u0": round(base_u, 3), "u1": round(umax, 3), "du": round(du, 3),
            "used": {m: round(used[m], 4 if m == "usd" else 0) for m in METRICS},
            "mix": mix_of(bm), "by_model": compact_bm(bm), "est": None, "chunks": [],
        }
        # chunks: consecutive slices of the window with at least chunk_du utilization rise
        chunks, ct, cu = [], base_t, base_u
        for p in pts:
            if p[0] > fin[0]:
                break
            if p[3] - cu >= cfg("chunk_du", key):
                chunks.append((round(p[3] - cu, 3), ledger.by_model(ct, p[0])))
                ct, cu = p[0], p[3]
        rec["chunks"] = [[d, compact_bm(b)] for d, b in chunks]
        if du < cfg("min_window_du", key) or used["usd"] <= 0:
            out.append(rec)
            continue
        q = DEFAULTS["quant"]
        est = {}
        for m in METRICS:
            v = used[m] / du
            lo, hi = used[m] / (du + q), used[m] / max(du - q, q)
            if len(chunks) >= 3:  # bootstrap over chunks (~68% interval)
                cs = [(sum_models(b)[m], d) for d, b in chunks]
                bs = []
                for _ in range(300):
                    pick = [cs[rng.randrange(len(cs))] for _ in cs]
                    bs.append(sum(c[0] for c in pick) / sum(c[1] for c in pick))
                lo, hi = min(lo, quantile(bs, 0.16)), max(hi, quantile(bs, 0.84))
            r = 2 if m == "usd" else 0
            est[m] = [round(v, r), round(lo, r), round(hi, r)]
        rec["est"] = est
        out.append(rec)
    return out


def summarize_bar(key, windows, rng):
    """Model-mix adjustment, possible limit changes and the current estimate for one bar."""
    weekly = key != "five_hour"
    res = {"key": key, "label": LABELS.get(key, key), "window_hours": window_len(key) / HOUR,
           "n_windows": len(windows), "n_estimates": 0, "changes": [], "current": None,
           "weights": None, "ref_model": None}
    pts = sorted([w for w in windows if w.get("est")], key=lambda w: w["t_last"])
    res["n_estimates"] = len(pts)
    if not pts:
        return res

    def adjusted(fits):
        for w in pts:
            w["adj"] = {}
            for m in METRICS:
                f = adjust_factor(w, fits.get(m), m)
                r = 2 if m == "usd" else 0
                w["adj"][m] = [round(x * f, r) for x in w["est"][m]]

    fits = {m: fit_weights(windows, m, rng) for m in METRICS}
    adjusted(fits)

    # possible limit changes, on the mix-adjusted $ series
    min_seg = 3 if weekly else 6
    series = [(w["t_last"], w["adj"]["usd"][0]) for w in pts]
    splits = detect_changes(series, min_seg)
    for k in splits:
        before = statistics.median(v for _, v in series[max(0, k - 20):k])
        after = statistics.median(v for _, v in series[k:k + 20])
        res["changes"].append({"t": int((pts[k - 1]["t_last"] + pts[k]["start"]) / 2),
                               "t_from": pts[k - 1]["t_last"], "t_to": pts[k]["start"],
                               "before_usd": round(before, 2), "after_usd": round(after, 2),
                               "rel": round(after / before - 1, 3)})
    seg0 = splits[-1] if splits else 0

    # current = windows since the last detected change; at most the last 14 days (5h) or
    # the last 4 windows (weekly)
    recent = pts[seg0:]
    if weekly:
        recent = recent[-4:]
    else:
        cutoff = recent[-1]["t_last"] - 14 * DAY
        recent = [w for w in recent if w["t_last"] >= cutoff]
    # refit the per-model weights on the current period when it has enough data
    rkeys = {(w["reset"], w["seg"]) for w in recent}
    cur_windows = [w for w in windows if (w["reset"], w["seg"]) in rkeys]
    if len(cur_windows) >= (2 if weekly else 10) and seg0:
        fits_now = {m: fit_weights(cur_windows, m, rng) or fits[m] for m in METRICS}
    else:
        fits_now = fits

    cur = {"n": len(recent), "t_from": recent[0]["start"], "t_to": recent[-1]["t_last"]}
    for kind in ("adj", "est"):
        cur[kind] = {}
        for m in METRICS:
            vals = [w[kind][m][0] for w in recent]
            med = statistics.median(vals)
            if len(vals) >= 3:
                bs = sorted(statistics.median([vals[rng.randrange(len(vals))] for _ in vals])
                            for _ in range(2000))
                lo, hi = quantile(bs, 0.025), quantile(bs, 0.975)
            else:
                lo = min(min(w[kind][m][1] for w in recent), med)
                hi = max(max(w[kind][m][2] for w in recent), med)
            r = 2 if m == "usd" else 0
            cur[kind][m] = {"median": round(med, r), "lo": round(lo, r), "hi": round(hi, r),
                            "p25": round(quantile(vals, 0.25), r), "p75": round(quantile(vals, 0.75), r)}
    res["current"] = cur

    # per-model view: 100% of the bar if you used only that model
    f = fits_now.get("usd")
    if f:
        ref = f["ref"]
        res["ref_model"] = ref
        weights = {}
        for g in f["groups"]:
            k = f["k"][g]
            rel = {"k_rel": round(k / f["k"][ref], 3) if f["k"][ref] else None,
                   "share": f["share"][g], "models": f["groups"][g]}
            for m in METRICS:
                fm = fits_now.get(m) or f
                c = cur["adj"][m]
                if fm["k"].get(g, 0) > 0 and fm["k"].get(ref, 0) > 0:
                    ratio = fm["k"][ref] / fm["k"][g]
                    rr = sorted((b[ref] / b[g]) if b.get(g, 0) > 0 else float("inf") for b in fm["boot"])
                    rlo, rhi = quantile(rr, 0.025), quantile(rr, 0.975)
                    r = 2 if m == "usd" else 0
                    rel[m] = {"median": round(c["median"] * ratio, r),
                              "lo": round(c["lo"] * min(rlo, ratio), r),
                              "hi": None if math.isinf(rhi) or math.isnan(rhi) else round(c["hi"] * max(rhi, ratio), r)}
                else:
                    rel[m] = None  # this model does not (measurably) count towards this bar
            weights[g] = rel
        res["weights"] = weights
        res["fit_rows"] = f["rows"]
    return res


# ---------------------------------------------------------------- main

def default_transcript_globs():
    roots = []
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        roots.append(os.environ["CLAUDE_CONFIG_DIR"])
    roots += ["~/.claude", "~/.config/claude"]
    return [os.path.join(r, "projects", "**", "*.jsonl") for r in roots]


def env_list(name):
    v = os.environ.get(name)
    return [x for x in v.split(os.pathsep) if x] if v else []


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--transcripts", action="append", metavar="GLOB",
                    help="Claude Code transcript glob (repeatable). Env LIMIT_TRACKER_TRANSCRIPTS. "
                         "Default: $CLAUDE_CONFIG_DIR, ~/.claude, ~/.config/claude /projects/**/*.jsonl")
    ap.add_argument("--streams", action="append", metavar="GLOB",
                    help="stream-json log glob with rate_limit_event lines (repeatable). Env LIMIT_TRACKER_STREAMS")
    ap.add_argument("--samples", action="append", metavar="FILE",
                    help="status line sampler log (repeatable). Env LIMIT_TRACKER_SAMPLES. "
                         "Default ~/.local/state/claude-limit-tracker/samples.jsonl")
    ap.add_argument("--out", default=os.environ.get("LIMIT_TRACKER_OUT", os.path.join(HERE, "..", "docs", "data")),
                    help="output directory (default: docs/data next to this script)")
    ap.add_argument("--prices", default=os.path.join(HERE, "prices.json"))
    ap.add_argument("--plan", default=os.environ.get("LIMIT_TRACKER_PLAN"),
                    help="optional public plan label shown on the site, e.g. 'Max 20x'")
    ap.add_argument("--rebuild", action="store_true",
                    help="recompute every window instead of keeping frozen (closed) windows from the previous run")
    a = ap.parse_args(argv)

    tglobs = a.transcripts or env_list("LIMIT_TRACKER_TRANSCRIPTS") or default_transcript_globs()
    sglobs = a.streams or env_list("LIMIT_TRACKER_STREAMS")
    pfiles = a.samples or env_list("LIMIT_TRACKER_SAMPLES") or ["~/.local/state/claude-limit-tracker/samples.jsonl"]

    t0 = time.time()
    calls, samples = Calls(), []
    tfiles, sfiles, pf = expand(tglobs), expand(sglobs), expand(pfiles)
    log("reading %d transcript files, %d stream logs, %d sampler logs" % (len(tfiles), len(sfiles), len(pf)))
    cs_runs = load_transcripts(tfiles, calls)
    runs = load_streams(sfiles, calls, samples)
    load_sampler(pf, samples)
    if not calls.by_id:
        log("no calls found; check --transcripts")
        return 1

    pricer = Pricer(a.prices)
    ledger = Ledger(calls, pricer)
    cover_start = ledger.t[0] if ledger.t else None
    now = time.time()

    # previous output: closed windows are frozen so history survives transcript cleanup
    out_path = os.path.join(a.out, "estimates.json")
    prev = {}
    if os.path.exists(out_path):
        try:
            with open(out_path, encoding="utf-8") as fh:
                prev = json.load(fh)
        except ValueError:
            prev = {}
    prev_windows = {}
    for key, bar in (prev.get("bars") or {}).items():
        for w in bar.get("windows", []):
            prev_windows[(w["bar"], w["reset"], w["seg"])] = w
    prev_events = prev.get("events") or []

    rng = random.Random(1)
    events = []
    windows = {}
    for (key, reset), ss in sorted(group_samples(samples).items()):
        for w in analyse_window(key, reset, ss, ledger, cover_start, rng, events):
            windows[(w["bar"], w["reset"], w["seg"])] = w
    kept = 0
    if not a.rebuild:
        for k, w in prev_windows.items():
            if w.get("frozen") and (k not in windows or windows[k]["n"] <= w["n"]):
                if k not in windows or windows[k]["n"] < w["n"]:
                    kept += 1  # source logs for this window are gone (or thinner): keep the old result
                windows[k] = w
    for w in windows.values():
        if w["reset"] < now - HOUR:
            w["frozen"] = True

    # merge early-reset events (same bar within 10 minutes = same event)
    allev = []
    for e in prev_events + events:
        if not any(x["type"] == e["type"] and x["bar"] == e["bar"] and abs(x["t1"] - e["t1"]) < 600 for x in allev):
            allev.append(e)
    allev.sort(key=lambda e: e["t1"])

    bars = {}
    for key in sorted({k[0] for k in windows}, key=lambda k: (k != "five_hour", k != "seven_day", k)):
        ws = sorted((w for k, w in windows.items() if k[0] == key), key=lambda w: (w["reset"], w["seg"]))
        bar = summarize_bar(key, ws, random.Random(7))
        bar["windows"] = ws
        bars[key] = bar

    # diagnostics (aggregate only)
    stream_only = sum(1 for c in calls.by_id.values() if c["src"] == {"s"})
    cost_cache = {}

    def logged_cost(ids):
        tot = 0.0
        for i in ids:
            if i not in cost_cache:
                c = calls.by_id.get(i)
                cost_cache[i] = (pricer.cost(c) or 0.0) if c else 0.0
            tot += cost_cache[i]
        return tot

    def overhead(runs_):
        """How much more Claude Code says it spent than its logged calls add up to."""
        pairs = [(cli, logged_cost(ids)) for cli, ids in runs_]
        pairs = [(c, o) for c, o in pairs if o > 0.5 and c > 0]
        if not pairs:
            return None
        ratios = [c / o for c, o in pairs]
        ok = [(c, o) for c, o in pairs if 0.8 <= c / o <= 3.0]
        return {"sessions": len(pairs), "median_ratio": round(statistics.median(ratios), 3),
                "p25": round(quantile(ratios, 0.25), 3), "p75": round(quantile(ratios, 0.75), 3),
                "pooled_ratio": round(sum(c for c, _ in ok) / sum(o for _, o in ok), 3) if ok else None}

    gap = overhead(runs)
    gap_t = overhead(cs_runs)
    stimes = [s[0] for s in samples]
    total = ledger.total()
    result = {
        "schema": SCHEMA,
        "generated_at": iso(now),
        "plan": a.plan or None,
        "prices": {"source": pricer.p.get("_source"), "checked": pricer.p.get("_checked")},
        "coverage": {
            "calls": len(ledger.t),
            "first_call": iso(ledger.t[0]), "last_call": iso(ledger.t[-1]),
            "samples": len(samples),
            "first_sample": iso(min(stimes)) if stimes else None,
            "last_sample": iso(max(stimes)) if stimes else None,
            "sample_sources": sorted({s[4] for s in samples}),
            "usd_total": round(total["usd"], 2),
            "model_mix": mix_of(ledger.by_model(ledger.t[0] - 1, ledger.t[-1])),
        },
        "gaps": {
            "stream_only_calls": stream_only,
            "synthetic_skipped": calls.skipped_synthetic,
            "unknown_models": pricer.unknown,
            "cli_cost_vs_logged": {"stream_results": gap, "transcript_cost_state": gap_t},
            "kept_from_previous_runs": kept,
        },
        "events": allev,
        "bars": bars,
    }
    os.makedirs(a.out, exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(result, fh, separators=(",", ":"), sort_keys=False)
        fh.write("\n")
    os.replace(tmp, out_path)

    log("done in %.1fs: %d calls (%d stream-only), %d samples, $%.0f API-equivalent"
        % (time.time() - t0, len(ledger.t), stream_only, len(samples), total["usd"]))
    for key, bar in bars.items():
        c = bar["current"]
        if not c:
            log("  %-30s %3d windows, no estimate yet" % (bar["label"], bar["n_windows"]))
            continue
        a_, e_ = c["adj"]["usd"], c["est"]["usd"]
        log("  %-30s %3d windows, %3d estimates, ref model %s" % (bar["label"], bar["n_windows"], bar["n_estimates"], bar["ref_model"]))
        log("      mix-adjusted: 100%% ~ $%.2f (95%% CI %.2f-%.2f, IQR %.2f-%.2f), %.2fM weighted tok"
            % (a_["median"], a_["lo"], a_["hi"], a_["p25"], a_["p75"], c["adj"]["wtok"]["median"] / 1e6))
        log("      as used:      100%% ~ $%.2f (95%% CI %.2f-%.2f, IQR %.2f-%.2f)"
            % (e_["median"], e_["lo"], e_["hi"], e_["p25"], e_["p75"]))
        for g, wt in (bar["weights"] or {}).items():
            u = wt["usd"]
            log("      %-18s share %5.1f%%  k_rel %-6s 100%% if only this: %s"
                % (g, wt["share"] * 100, wt["k_rel"],
                   ("$%.2f (%.2f-%s)" % (u["median"], u["lo"], "inf" if u["hi"] is None else "%.2f" % u["hi"])) if u else "not counted"))
        for ch in bar["changes"]:
            log("      possible change ~%s: $%.2f -> $%.2f (%+.0f%%)" % (iso(ch["t"]), ch["before_usd"], ch["after_usd"], ch["rel"] * 100))
    if pricer.unknown:
        log("  unpriced models (excluded):", pricer.unknown)
    for name, g in (("stream results", gap), ("transcript cost-state", gap_t)):
        if g:
            log("  CLI-reported / logged cost (%s): median %.3f (IQR %.3f-%.3f), pooled %s, %d sessions"
                % (name, g["median_ratio"], g["p25"], g["p75"], g["pooled_ratio"], g["sessions"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

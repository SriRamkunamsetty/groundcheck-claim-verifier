"""Grounded narrative generation + claim-level verification.

Rules: (1) every number must trace to a Fact; (2) the number must be attached to the RIGHT claim
(the 24h figure described as "72 hours" fails); (3) no unhedged causal language or hype.
Claim attribution is heuristic: each number is checked against metric cues found in its own clause."""
import re
from dataclasses import dataclass, field

from .metrics import Fact, anomalies

MARK = re.compile(r"\[\[f:([a-z0-9_]+)\]\]")
NUM = re.compile(r"(?<![\w.])(\d+(?:,\d{3})*(?:\.\d+)?)(\s?%|[KMB]\b)?(\s?-?(?i:hours?|hrs?|h|days?|d)\b)?")
_MULT = {"K": 1e3, "M": 1e6, "B": 1e9}
CLAUSE = re.compile(r"[;()]|(?<=[.!?])\s+|,\s")

CUES = {
    "velocity_24h": r"\b24\s?-?\s?(?:h|hr|hrs|hours?)\b|first day",
    "velocity_72h": r"\b72\s?-?\s?(?:h|hr|hrs|hours?)\b|three days|\b3 days",
    "velocity_7d": r"\b7\s?-?\s?(?:d|days?)\b|seven days|first week|\bweek\b|\b168\b",
    "eng_rate": r"engagement|interaction rate",
    "sov_7d": r"share of voice|\bsov\b",
    "benchmark_72h": r"percentile",
    "benchmark_n": r"\btitles?\b",
    "n_posts": r"\b(?:posts?|videos?|uploads?)\b",
    "anomaly_day": r"\bday\b",
    "anomaly_views": r"spike|unusual|anomal|peak",
}
OVERCLAIM = re.compile(r"\b(proves?|proved|clearly (?:drove|caused)|definitively|undoubtedly|caused by|directly (?:led|caused)|drove this)\b", re.I)
HYPE = re.compile(r"\b(exploded|skyrocket\w*|viral sensation|massive|record-breaking|unprecedented)\b", re.I)


def _cue(fid):
    if fid in CUES:
        return CUES[fid]
    if fid.startswith("anomaly_"):
        return CUES["anomaly_day" if fid.endswith("_day") else "anomaly_views"]
    return None


def _ordinal(n):
    n = int(round(n))
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def fmt(f: Fact) -> str:
    v = f.value
    if f.unit == "ratio":
        return f"{v * 100:.2f}%"
    if f.unit == "pct":
        return _ordinal(v)
    if f.unit == "count":
        return f"{int(round(v))}"
    for lim, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= lim:
            return f"{v / lim:.3g}{suf}"
    return f"{v:.0f}"


def render(text, facts):
    by_id = {f.id: f for f in facts}
    return MARK.sub(lambda m: fmt(by_id[m.group(1)]) if m.group(1) in by_id else m.group(0), text)


def _claims(text):
    """Yield (match, normalised value, is_pct) for each numeric claim; window labels and bare years are skipped."""
    for m in NUM.finditer(text):
        raw, suf, unit = m.group(1), (m.group(2) or "").strip(), m.group(3)
        if unit:
            continue
        x = float(raw.replace(",", ""))
        if not suf and "," not in raw and "." not in raw and 1900 <= x <= 2100:
            continue
        yield m, (x / 100 if suf == "%" else x * _MULT.get(suf, 1)), suf == "%"


def numbers_in(text):
    """Numeric claims, normalised (12.5% -> 0.125, 1.2M -> 1.2e6)."""
    return [(m.group(0).strip(), x, p) for m, x, p in _claims(text)]


def _supports(x, is_pct, fact, rel_tol=0.01):
    cands = [fact.value] + ([fact.value * 100] if fact.unit == "ratio" and not is_pct else [])
    return any(abs(x - c) <= rel_tol * abs(c) + 1e-9 for c in cands)


def states(text, fact) -> bool:
    """True if `text` states this fact's value in a clause that is about this metric."""
    rx = _cue(fact.id)
    for clause in CLAUSE.split(text):
        if rx and not re.search(rx, clause, re.I):
            continue
        if any(_supports(x, p, fact) for _, x, p in numbers_in(clause)):
            return True
    return False


def style_flags(text):
    flags = []
    for m in OVERCLAIM.finditer(text):
        if not re.search(r"\b(not|cannot|can't|doesn't|does not|n't|no)\s+(?:\w+\s+){0,2}$", text[max(0, m.start() - 30):m.start()], re.I):
            flags.append({"type": "causal_overclaim", "text": m.group(0)})
    flags += [{"type": "hype", "text": m.group(0)} for m in HYPE.finditer(text)]
    return flags


@dataclass
class Report:
    n_numbers: int = 0
    unsupported: list = field(default_factory=list)
    misattributed: list = field(default_factory=list)
    unknown_markers: list = field(default_factory=list)
    style_flags: list = field(default_factory=list)

    @property
    def groundedness(self):
        return 1.0 if not self.n_numbers else 1 - len(self.unsupported) / self.n_numbers

    @property
    def claim_accuracy(self):
        return 1.0 if not self.n_numbers else 1 - (len(self.unsupported) + len(self.misattributed)) / self.n_numbers

    @property
    def ok(self):
        return not (self.unsupported or self.misattributed or self.unknown_markers or self.style_flags)

    def reasons(self):
        r = [f"unsupported number {u}" for u in self.unsupported]
        r += [f"'{m['claim']}' is the value of {m['matches']} but the clause is about {m['clause_cues']}" for m in self.misattributed]
        r += [f"unknown marker {m}" for m in self.unknown_markers]
        r += [f"{s['type']}: '{s['text']}'" for s in self.style_flags]
        return r


def verify(text, facts, ignore=()) -> Report:
    rep = Report(unknown_markers=[m for m in MARK.findall(text) if m not in {f.id for f in facts}])
    rendered = render(text, facts)
    for name in ignore:  # a title like "Apollo 13" must not count as a numeric claim
        rendered = rendered.replace(name, " ")
    rep.style_flags = style_flags(rendered)
    for clause in CLAUSE.split(rendered):
        cues = {f.id for f in facts if _cue(f.id) and re.search(_cue(f.id), clause, re.I)}
        for raw, x, is_pct in numbers_in(clause):
            rep.n_numbers += 1
            sup = {f.id for f in facts if _supports(x, is_pct, f)}
            if not sup:
                rep.unsupported.append(raw)
            elif cues and not (sup & cues):
                rep.misattributed.append({"claim": raw, "clause": clause.strip(), "matches": sorted(sup), "clause_cues": sorted(cues)})
    return rep


def annotate(text, facts, ignore=()):
    """Display segments [{text, status, detail}] with status plain|ok|unsupported|misattributed|style.
    Uses exactly the same rules as verify(), so the highlighted view can never disagree with the verdict."""
    rendered = render(text, facts)
    masked = rendered
    for name in ignore:                       # same-length mask keeps character offsets aligned
        masked = masked.replace(name, " " * len(name))
    seps = [(m.start(), m.end()) for m in CLAUSE.finditer(masked)]
    def clause_at(pos):
        lo = max([e for s_, e in seps if e <= pos], default=0)
        hi = min([s_ for s_, e in seps if s_ >= pos], default=len(masked))
        return masked[lo:hi]
    marks = []
    for m, x, is_pct in _claims(masked):
        clause = clause_at(m.start())
        cues = {f.id for f in facts if _cue(f.id) and re.search(_cue(f.id), clause, re.I)}
        sup = {f.id for f in facts if _supports(x, is_pct, f)}
        if not sup:
            marks.append((m.start(), m.end(), "unsupported", "no computed fact has this value"))
        elif cues and not (sup & cues):
            marks.append((m.start(), m.end(), "misattributed", f"this is the value of {sorted(sup)}, but the clause is about {sorted(cues)}"))
        else:
            marks.append((m.start(), m.end(), "ok", f"matches {sorted(sup)}"))
    for f in style_flags(masked):
        for m in re.finditer(re.escape(f["text"]), masked):
            if not any(a <= m.start() < b for a, b, _, _ in marks):
                marks.append((m.start(), m.end(), "style", f["type"].replace("_", " ")))
                break
    segs, pos = [], 0
    for a, b, status, detail in sorted(marks):
        if a < pos:
            continue
        if a > pos:
            segs.append({"text": rendered[pos:a], "status": "plain", "detail": ""})
        segs.append({"text": rendered[a:b], "status": status, "detail": detail})
        pos = b
    if pos < len(rendered):
        segs.append({"text": rendered[pos:], "status": "plain", "detail": ""})
    return segs


def template_narrative(title, facts, conn=None, title_id=None):
    """Deterministic, fully grounded narrative with a 'why' and 'what next' layer."""
    have = {f.id for f in facts}
    if "velocity_72h" in have:
        s = [f"{title}: [[f:velocity_72h]] views in the first 72 hours across [[f:n_posts]] posts"
             + (" (7-day total [[f:velocity_7d]])." if "velocity_7d" in have else ".")]
    else:
        s = [f"{title}: no 72-hour window observed yet."]
    if "eng_rate" in have:
        s.append("Engagement rate is [[f:eng_rate]] of views.")
    if "sov_7d" in have:
        s.append("Share of voice in its competitive set is [[f:sov_7d]] of 7-day views.")
    if "benchmark_72h" in have:
        s.append("That puts 72-hour velocity at the [[f:benchmark_72h]] percentile among [[f:benchmark_n]] titles in its genre (small samples move this a lot).")
    if "anomaly_1_day" in have:
        s.append("A statistically unusual spike peaked on day [[f:anomaly_1_day]] with [[f:anomaly_1_views]] views.")
        ev = anomalies(conn, title_id)[0]["events"] if conn is not None else []
        s.append(f"It aligns with a logged event ({'; '.join(ev)}), which fits the timing but does not prove cause; a controlled repeat would test it." if ev else
                 "No logged event explains it, so check for organic creator pick-up before attributing it to paid media.")
    return " ".join(s)


def ignore_list(conn, title_id, title):
    """Strings that may contain digits but are names, not claims: the title and its logged event labels."""
    labels = [r["label"] for r in conn.execute("SELECT label FROM events WHERE title_id=?", (title_id,))]
    return [x for x in [title, *labels] if x]


def build_prompt(title, facts):
    lines = "\n".join(f"- {f.id}: {f.label} = {fmt(f)}  [{f.query}]" for f in facts)
    return ("You are a senior social media analyst. Write a 4-6 sentence performance summary for "
            f"'{title}' with one 'why' and one 'what next'.\nRules: use ONLY these facts; cite every number "
            "as [[f:fact_id]] and never write a number yourself; say which window/metric each number is; "
            "describe timing alignment, never proven causation; no hype words.\n\nFACTS:\n" + lines)


def llm_narrative(title, facts, llm):
    """llm: callable(prompt)->str. Output is rendered; callers should verify() it."""
    return render(llm(build_prompt(title, facts)), facts)

"""Evaluation harness: score any narrator (template, LLM, or baseline) on numeric recall and
hallucination rate, and measure how far an LLM judge agrees with human labels (Cohen's kappa)."""
from collections import Counter
from dataclasses import dataclass

from .metrics import build_facts
from .narrator import ignore_list, llm_narrative, render, states, template_narrative, verify


@dataclass
class Case:
    name: str
    title_id: int
    peer_ids: tuple
    must_state: tuple  # fact ids whose value must appear in the narrative


def default_cases(conn):
    ids = [r["id"] for r in conn.execute("SELECT id FROM titles ORDER BY id")]
    cases = []
    for t in ids:
        name = conn.execute("SELECT name FROM titles WHERE id=?", (t,)).fetchone()["name"]
        have = {f.id for f in build_facts(conn, t, [i for i in ids if i != t])}
        need = tuple(k for k in ("velocity_72h", "eng_rate", "sov_7d", "benchmark_72h") if k in have)
        cases.append(Case(name, t, tuple(i for i in ids if i != t), need))
    return cases


def grounded_narrator(conn, case):
    facts = build_facts(conn, case.title_id, case.peer_ids)
    return render(template_narrative(case.name, facts, conn, case.title_id), facts)


def inflating_baseline(conn, case):
    """Deliberately flawed baseline: rounds up and invents a growth stat. Proves the harness has teeth."""
    facts = build_facts(conn, case.title_id, case.peer_ids)
    d = {f.id: f for f in facts}
    v = d["velocity_72h"].value * 1.25
    return f"{case.name} exploded with {v / 1e6:.2f}M views in 72 hours, up 340% versus the last release."


def swapped_window_baseline(conn, case):
    """Flawed baseline: says '72 hours' but cites the 24-hour number. Only claim-level checking catches it."""
    facts = build_facts(conn, case.title_id, case.peer_ids)
    text = template_narrative(case.name, facts, conn, case.title_id).replace("[[f:velocity_72h]]", "[[f:velocity_24h]]")
    return render(text, facts)


def llm_narrator(llm):
    def narrate(conn, case):
        return llm_narrative(case.name, build_facts(conn, case.title_id, case.peer_ids), llm)
    return narrate


def run_eval(conn, cases, narrators):
    results = {}
    for name, fn in narrators.items():
        recalls, n_nums, n_bad, n_mis, n_style, passed = [], 0, 0, 0, 0, 0
        for c in cases:
            facts = build_facts(conn, c.title_id, c.peer_ids)
            by_id = {f.id: f for f in facts}
            text = fn(conn, c)
            hits = [states(text, by_id[k]) for k in c.must_state]
            rep = verify(text, facts, ignore=ignore_list(conn, c.title_id, c.name))
            recall = sum(hits) / len(hits) if hits else 1.0
            recalls.append(recall)
            n_nums, n_bad = n_nums + rep.n_numbers, n_bad + len(rep.unsupported)
            n_mis, n_style = n_mis + len(rep.misattributed), n_style + len(rep.style_flags)
            passed += recall == 1.0 and rep.ok
        results[name] = {"cases": len(cases), "numeric_recall": sum(recalls) / len(recalls),
                         "hallucination_rate": (n_bad / n_nums) if n_nums else 0.0,
                         "misattribution_rate": (n_mis / n_nums) if n_nums else 0.0,
                         "style_flags": n_style,
                         "pass_rate": passed / len(cases)}
    return results


def cohen_kappa(a, b):
    """Agreement between two raters beyond chance (e.g. human labels vs an LLM judge)."""
    if len(a) != len(b) or not a:
        raise ValueError("label lists must be the same non-zero length")
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(a) | set(b)) / (n * n)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)

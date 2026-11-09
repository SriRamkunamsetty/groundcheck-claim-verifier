"""Golden set: generate summaries (some with injected defects), label them blind as a human,
then measure how well automated judges (rules, LLM) agree with YOUR labels (Cohen's kappa).
Human labels are never auto-filled. The answer key of injected defects is stored separately so
you label blind."""
import json
import random
import re
from dataclasses import asdict

from .db import now_iso
from .evals import cohen_kappa, default_cases
from .metrics import Fact, build_facts
from .narrator import fmt, ignore_list, render, template_narrative, verify

DEFECTS = ("number_wrong", "wrong_claim_attribution", "unsupported_claim", "causal_overclaim", "other")
CAUSAL_OK = "which fits the timing but does not prove cause; a controlled repeat would test it"


def _jl_read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return [json.loads(l) for l in fh if l.strip()]
    except FileNotFoundError:
        return []


def _jl_write(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in rows)


def case_variants(conn, c):
    """One clean summary plus four injected-defect variants for a case: [{title, ignore, facts, text, _injected}]."""
    facts = build_facts(conn, c.title_id, c.peer_ids)
    fj, by_id = [asdict(f) for f in facts], {f.id: f for f in facts}
    marked = template_narrative(c.name, facts, conn, c.title_id)
    ign = ignore_list(conn, c.title_id, c.name)
    mk = lambda t, d: {"title": c.name, "ignore": ign, "facts": fj, "text": render(t, facts), "_injected": d}
    inflated = fmt(Fact("x", "x", by_id["velocity_72h"].value * 1.25, "views", ""))
    return [mk(marked, None),
            mk(marked.replace("[[f:velocity_72h]]", "[[f:velocity_24h]]"), "wrong_claim_attribution"),
            mk(marked.replace("[[f:velocity_72h]]", inflated, 1), "number_wrong"),
            mk(marked + " Views are up 340% week over week.", "unsupported_claim"),
            mk(marked.replace(CAUSAL_OK, "which proves the TV spot drove the spike") if CAUSAL_OK in marked
               else marked + " The campaign clearly caused this result.", "causal_overclaim")]


def make_items(conn, n=40, seed=11):
    rng, clean, bad = random.Random(seed), [], []
    for c in default_cases(conn):
        for it in case_variants(conn, c):
            (clean if it["_injected"] is None else bad).append(it)
    rng.shuffle(clean); rng.shuffle(bad)
    pick = clean[: n // 2] + bad[: n - min(n // 2, len(clean))]
    rng.shuffle(pick)
    items, key = [], []
    for i, it in enumerate(pick, 1):
        gid = f"g{i:03d}"
        key.append({"id": gid, "injected": it.pop("_injected")})
        items.append({"id": gid, **it})
    return items, key


def generate(conn, out_dir, n=40, seed=11):
    import os
    os.makedirs(out_dir, exist_ok=True)
    items, key = make_items(conn, n, seed)
    _jl_write(os.path.join(out_dir, "items.jsonl"), items)
    _jl_write(os.path.join(out_dir, "answer_key.jsonl"), key)
    return len(items)


def label_items(items_path, labels_path, labeler="me", input_fn=input, out=print):
    """Interactive blind labelling. Resumable. Verdict p=pass, f=fail, s=skip, q=quit."""
    items, labels = _jl_read(items_path), _jl_read(labels_path)
    done = {l["id"] for l in labels}
    todo = [i for i in items if i["id"] not in done]
    out(f"{len(done)} labelled, {len(todo)} to go. PASS only if every number is the right metric/window, "
        "nothing is unsupported, and causation is not asserted as proven.")
    added = 0
    for it in todo:
        out(f"\n[{it['id']}] {it['title']}\n{it['text']}\n  facts: " + "; ".join(f"{f['id']}={fmt(Fact(**f))}" for f in it["facts"]))
        v = input_fn("verdict [p]ass/[f]ail/[s]kip/[q]uit: ").strip().lower()
        if v == "q":
            break
        if v not in ("p", "f"):
            continue
        defect = None
        if v == "f":
            d = input_fn("defect " + ", ".join(f"{k}={x}" for k, x in enumerate(DEFECTS, 1)) + ": ").strip()
            defect = DEFECTS[int(d) - 1] if d.isdigit() and 1 <= int(d) <= len(DEFECTS) else "other"
        labels.append({"id": it["id"], "verdict": "pass" if v == "p" else "fail", "defect": defect,
                       "labeler": labeler, "labeled_at": now_iso()})
        _jl_write(labels_path, labels)
        added += 1
    return added


def rules_judge(item):
    facts = [Fact(**f) for f in item["facts"]]
    rep = verify(item["text"], facts, ignore=item.get("ignore") or [item["title"]])
    return ("pass" if rep.ok else "fail"), "; ".join(rep.reasons())


JUDGE_SYSTEM = "You are a strict reviewer of social-media analytics summaries. Reply with JSON only."


def llm_judge(llm):
    def judge(item):
        facts = "\n".join(f"- {f['id']}: {f['label']} = {fmt(Fact(**f))}" for f in item["facts"])
        prompt = ("FACTS (ground truth):\n" + facts + f"\n\nSUMMARY:\n{item['text']}\n\n"
                  "PASS only if: every number equals the fact for the metric AND window it describes; no claim lacks "
                  "a supporting fact; causation is not asserted as proven; no hype. Otherwise FAIL.\n"
                  'Reply as JSON: {"verdict":"pass"|"fail","defect":"number_wrong|wrong_claim_attribution|unsupported_claim|causal_overclaim|other|none","reason":"..."}')
        try:
            raw = llm(prompt, system=JUDGE_SYSTEM, max_tokens=300)
            m = re.search(r"\{.*\}", raw, re.S)
            data = json.loads(m.group(0))
            v = str(data.get("verdict", "")).lower()
            return (v, str(data.get("reason", ""))) if v in ("pass", "fail") else ("error", "bad verdict")
        except Exception as exc:
            return "error", str(exc)[:120]
    return judge


def _confusion(h, j):
    tp = sum(a == "fail" and b == "fail" for a, b in zip(h, j))
    fp = sum(a == "pass" and b == "fail" for a, b in zip(h, j))
    fn = sum(a == "fail" and b == "pass" for a, b in zip(h, j))
    tn = sum(a == "pass" and b == "pass" for a, b in zip(h, j))
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision_fail": tp / (tp + fp) if tp + fp else None, "recall_fail": tp / (tp + fn) if tp + fn else None}


def calibrate(out_dir, judges):
    """Agreement vs HUMAN labels, plus detection of injected defects (needs no labels)."""
    import os
    items = {i["id"]: i for i in _jl_read(os.path.join(out_dir, "items.jsonl"))}
    labels = {l["id"]: l for l in _jl_read(os.path.join(out_dir, "labels.jsonl")) if l["id"] in items}
    key = {k["id"]: k["injected"] for k in _jl_read(os.path.join(out_dir, "answer_key.jsonl"))}
    res = {"n_human_labels": len(labels), "judges": {}}
    for name, judge in judges.items():
        preds = {i: judge(items[i]) for i in items}
        r = {"errors": sum(p[0] == "error" for p in preds.values())}
        valid = [i for i in labels if preds[i][0] in ("pass", "fail")]
        if len(valid) >= 2:
            h, j = [labels[i]["verdict"] for i in valid], [preds[i][0] for i in valid]
            r.update({"n_compared": len(valid), "kappa_vs_human": cohen_kappa(h, j),
                      "accuracy_vs_human": sum(a == b for a, b in zip(h, j)) / len(h), **_confusion(h, j)})
            r["missed_by_human_defect"] = {d: sum(labels[i]["defect"] == d and preds[i][0] == "pass" for i in valid) for d in DEFECTS}
        det = {}
        for i, inj in key.items():
            if preds[i][0] in ("pass", "fail"):
                b = det.setdefault(inj or "clean", [0, 0])
                b[0] += preds[i][0] == "fail"; b[1] += 1
        r["injected_defect_flag_rate"] = {k: v[0] / v[1] for k, v in det.items()}  # clean row = false-positive rate
        res["judges"][name] = r
    return res

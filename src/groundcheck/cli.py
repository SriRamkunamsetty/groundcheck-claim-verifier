"""GroundCheck command line. Run `groundcheck <command> -h` for options."""
import argparse
import json
import os

from . import __version__
from .db import connect
from .evals import default_cases, grounded_narrator, inflating_baseline, llm_narrator, run_eval, swapped_window_baseline
from .facts_io import load_facts, uncued
from .golden import case_variants
from .metrics import build_facts
from .narrator import annotate, ignore_list, verify


def open_db(a):
    if getattr(a, "demo", False):
        from .sample_data import seed_all
        conn = connect(check_same_thread=False)
        seed_all(conn)
        return conn
    return connect(a.db, check_same_thread=False)


def _marked(text, facts, ignore):
    out = []
    for s in annotate(text, facts, ignore):
        out.append(s["text"] if s["status"] in ("plain", "ok") else f"[[{s['text']} <- {s['status'].upper()}]]")
    return "".join(out)


def build_parser():
    p = argparse.ArgumentParser(prog="groundcheck", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=os.environ.get("DATABASE_URL", "groundcheck.db"), help="SQLite path or postgresql:// DSN")
    common.add_argument("--demo", action="store_true", help="use in-memory SYNTHETIC demo data")
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="verify ANY summary against a facts file (exit 1 if it fails)")
    v.add_argument("--facts", required=True); g = v.add_mutually_exclusive_group(required=True)
    g.add_argument("--text"); g.add_argument("--text-file"); v.add_argument("--ignore", action="append", default=[], help="strings to ignore (titles, event names)")
    sub.add_parser("demo", parents=[common], help="verify the five demo variants of one summary").add_argument("--title", default="Iron Meridian")
    e = sub.add_parser("eval", help="score narrators on the demo data"); e.add_argument("--llm", choices=["anthropic", "gemini"])
    gd = sub.add_parser("golden", parents=[common], help="human-labelled golden set: gen | label | calibrate")
    gd.add_argument("action", choices=["gen", "label", "calibrate"]); gd.add_argument("--dir", default="golden"); gd.add_argument("--n", type=int, default=40)
    gd.add_argument("--labeler", default="me"); gd.add_argument("--judge", choices=["rules", "llm", "both"], default="rules"); gd.add_argument("--provider", choices=["anthropic", "gemini"])
    gd.add_argument("--save", action="store_true"); gd.add_argument("--validation-dir", default="validation")
    k = sub.add_parser("check-llm", help="live smoke test of an LLM key"); k.add_argument("--provider", choices=["anthropic", "gemini"])
    w = sub.add_parser("serve", parents=[common], help="the claim bench"); w.add_argument("--host", default="127.0.0.1"); w.add_argument("--port", type=int, default=8000)
    api_cmd = sub.add_parser("serve-api", parents=[common], help="launch production FastAPI service with OpenAPI, metrics, and health probes")
    api_cmd.add_argument("--host", default="0.0.0.0")
    api_cmd.add_argument("--port", type=int, default=8000)
    api_cmd.add_argument("--workers", type=int, default=1)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        _dispatch(a)
    except (RuntimeError, ValueError, OSError) as exc:
        raise SystemExit(f"error: {exc}")


def _dispatch(a):
    if a.cmd == "verify":
        facts = load_facts(a.facts)
        text = a.text if a.text is not None else open(a.text_file, encoding="utf-8").read()
        rep = verify(text, facts, ignore=a.ignore)
        print(_marked(text, facts, a.ignore))
        print(f"\n{'PASS' if rep.ok else 'FAIL'}: {rep.n_numbers} numbers checked")
        for r in rep.reasons():
            print("  -", r)
        un = uncued(facts)
        if un:
            print(f'note: no cue pattern for {un}: numbers are checked for existence but not for the right metric (add a "cue" to the facts file)')
        raise SystemExit(0 if rep.ok else 1)
    if a.cmd == "demo":
        a.demo = True
        conn = open_db(a)
        case = next((c for c in default_cases(conn) if c.name == a.title), None)
        if not case:
            raise SystemExit(f"unknown title: {a.title}")
        facts = build_facts(conn, case.title_id, case.peer_ids)
        for it in case_variants(conn, case):
            rep = verify(it["text"], facts, ignore=it["ignore"])
            print(f"--- {it['_injected'] or 'clean'}: {'PASS' if rep.ok else 'FAIL'}")
            print(_marked(it["text"], facts, it["ignore"]))
            for r in rep.reasons():
                print("    ->", r)
            print()
    elif a.cmd == "eval":
        from .sample_data import seed
        conn = connect(); seed(conn)
        narrators = {"grounded": grounded_narrator, "swapped_window_baseline": swapped_window_baseline, "inflating_baseline": inflating_baseline}
        if a.llm:
            from .llm import get_llm
            narrators[f"llm:{a.llm}"] = llm_narrator(get_llm(a.llm))
        print(json.dumps(run_eval(conn, default_cases(conn), narrators), indent=2))
    elif a.cmd == "golden":
        from . import golden
        d = a.dir
        if a.action == "gen":
            print(f"wrote {golden.generate(open_db(a), d, a.n)} items to {d}/ (answer key stored separately; label blind)")
        elif a.action == "label":
            print(f"added {golden.label_items(os.path.join(d, 'items.jsonl'), os.path.join(d, 'labels.jsonl'), a.labeler)} labels")
        else:
            judges = {}
            if a.judge in ("rules", "both"):
                judges["rules"] = golden.rules_judge
            if a.judge in ("llm", "both"):
                from .llm import get_llm
                judges["llm"] = golden.llm_judge(get_llm(a.provider))
            res = golden.calibrate(d, judges)
            print(json.dumps(res, indent=2))
            if a.save:
                os.makedirs(a.validation_dir, exist_ok=True)
                with open(os.path.join(a.validation_dir, "golden_calibration.json"), "w", encoding="utf-8") as fh:
                    json.dump(res, fh, indent=2)
    elif a.cmd == "check-llm":
        from .llm import get_llm
        llm = get_llm(a.provider)
        print(f"{llm.name} ({llm.model}) replied: {llm('Reply with the single word: ok', max_tokens=10).strip()!r}")
    elif a.cmd == "serve":
        from .web import serve
        serve(open_db(a), a.host, a.port)
    elif a.cmd == "serve-api":
        import uvicorn
        from .api import create_app
        app = create_app(None if a.demo else a.db)
        uvicorn.run(app, host=a.host, port=a.port, workers=a.workers)


if __name__ == "__main__":
    main()

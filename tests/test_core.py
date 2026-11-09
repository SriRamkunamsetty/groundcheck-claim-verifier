import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from groundcheck import golden
from groundcheck.cli import main
from groundcheck.db import connect
from groundcheck.evals import cohen_kappa, default_cases, grounded_narrator, inflating_baseline, run_eval, swapped_window_baseline
from groundcheck.facts_io import load_facts
from groundcheck.golden import calibrate, generate, label_items, llm_judge, rules_judge
from groundcheck.llm import AnthropicLLM, GeminiLLM, LLMError, get_llm
from groundcheck.metrics import Fact, build_facts
from groundcheck.narrator import annotate, llm_narrative, numbers_in, render, style_flags, template_narrative, verify
from groundcheck.sample_data import seed, seed_all
from groundcheck.web import App, make_handler


def ctx():
    c = connect(); seed(c)
    return c, build_facts(c, 1, range(2, 13))


class ClaimLevel(unittest.TestCase):
    def test_swapped_window_is_now_caught(self):
        c, facts = ctx()
        v24 = next(f for f in facts if f.id == "velocity_24h")
        from groundcheck.narrator import fmt
        rep = verify(f"{fmt(v24)} views in the first 72 hours.", facts)
        self.assertEqual(rep.unsupported, [])                       # the number exists...
        self.assertEqual(len(rep.misattributed), 1)                 # ...but on the wrong metric
        self.assertEqual(rep.misattributed[0]["matches"], ["velocity_24h"])

    def test_correct_attribution_passes_and_names_with_digits_ignored(self):
        c, facts = ctx()
        v72 = next(f for f in facts if f.id == "velocity_72h")
        from groundcheck.narrator import fmt
        self.assertTrue(verify(f"Apollo 13 drew {fmt(v72)} views in the first 72 hours.", facts, ignore=["Apollo 13"]).ok)

    def test_overclaim_vs_hedged(self):
        self.assertTrue(style_flags("The spot proves the campaign worked."))
        self.assertEqual(style_flags("This does not prove cause."), [])
        self.assertTrue(style_flags("It exploded overnight."))

    def test_eval_catches_all_three_flaw_types(self):
        c, _ = ctx()
        res = run_eval(c, default_cases(c), {"g": grounded_narrator, "swap": swapped_window_baseline, "infl": inflating_baseline})
        self.assertEqual(res["g"]["pass_rate"], 1.0)
        self.assertEqual(res["swap"]["pass_rate"], 0.0)
        self.assertGreater(res["swap"]["misattribution_rate"], 0)
        self.assertEqual(res["swap"]["hallucination_rate"], 0.0)   # old check alone would have passed this one


class LLMAdapters(unittest.TestCase):
    def test_anthropic_request_and_parse_with_retry(self):
        seen, seq = [], [(529, "overloaded"), (200, json.dumps({"content": [{"type": "text", "text": "hi"}]}))]
        def fetch(url, headers, body):
            seen.append((url, headers, json.loads(body))); return seq.pop(0)
        llm = AnthropicLLM("k", fetch=fetch, sleep=lambda s: None)
        self.assertEqual(llm("q", system="s"), "hi")
        url, headers, body = seen[-1]
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers["x-api-key"], "k"); self.assertEqual(body["system"], "s")
        self.assertEqual(len(seen), 2)

    def test_gemini_parse_and_key_in_header_not_url(self):
        seen = []
        def fetch(url, headers, body):
            seen.append((url, headers)); return 200, json.dumps({"candidates": [{"content": {"parts": [{"text": "a"}, {"text": "b"}]}}]})
        self.assertEqual(GeminiLLM("secret", model="m", fetch=fetch)("q"), "ab")
        self.assertNotIn("secret", seen[0][0])

    def test_errors_and_config(self):
        with self.assertRaises(LLMError):
            AnthropicLLM("")
        with self.assertRaises(LLMError):
            AnthropicLLM("k", fetch=lambda *a: (401, "bad"), sleep=lambda s: None)("q")
        os.environ.pop("ANTHROPIC_API_KEY", None); os.environ.pop("GEMINI_API_KEY", None); os.environ.pop("LLM_PROVIDER", None)
        with self.assertRaises(LLMError):
            get_llm()

    def test_llm_narrative_is_verified(self):
        c, facts = ctx()
        good = lambda p: "[[f:velocity_72h]] views in the first 72 hours."
        bad = lambda p: "Views hit 9.9M in the first 72 hours."
        self.assertTrue(verify(llm_narrative("T", facts, good), facts).ok)
        self.assertEqual(verify(llm_narrative("T", facts, bad), facts).unsupported, ["9.9M"])


class Golden(unittest.TestCase):
    def test_generate_label_calibrate(self):
        c, _ = ctx()
        d = tempfile.mkdtemp()
        n = generate(c, d, n=20)
        self.assertEqual(n, 20)
        items = golden._jl_read(os.path.join(d, "items.jsonl"))
        self.assertNotIn("_injected", items[0]); self.assertNotIn("injected", items[0])   # blind labelling
        key = {k["id"]: k["injected"] for k in golden._jl_read(os.path.join(d, "answer_key.jsonl"))}
        # scripted 'human' that agrees with the key for the first 10 items, to exercise the pipeline
        answers = []
        for it in items[:10]:
            answers += ["p"] if key[it["id"]] is None else ["f", "1"]
        it_ans = iter(answers)
        added = label_items(os.path.join(d, "items.jsonl"), os.path.join(d, "labels.jsonl"), input_fn=lambda _: next(it_ans, "q"), out=lambda *a: None)
        self.assertEqual(added, 10)
        # resumable: labelled items are not shown again
        self.assertEqual(label_items(os.path.join(d, "items.jsonl"), os.path.join(d, "labels.jsonl"), input_fn=lambda _: "q", out=lambda *a: None), 0)
        res = calibrate(d, {"rules": rules_judge})
        r = res["judges"]["rules"]
        self.assertEqual(res["n_human_labels"], 10)
        self.assertGreaterEqual(r["kappa_vs_human"], 0.99)                 # rules judge agrees with the key-consistent labels
        self.assertEqual(r["injected_defect_flag_rate"]["clean"], 0.0)     # no false positives on clean summaries
        for k in ("wrong_claim_attribution", "number_wrong", "unsupported_claim", "causal_overclaim"):
            if k in r["injected_defect_flag_rate"]:
                self.assertEqual(r["injected_defect_flag_rate"][k], 1.0, k)

    def test_llm_judge_parses_and_reports_errors(self):
        c, _ = ctx()
        d = tempfile.mkdtemp(); generate(c, d, n=4)
        item = golden._jl_read(os.path.join(d, "items.jsonl"))[0]
        self.assertEqual(llm_judge(lambda p, **k: 'ok {"verdict":"FAIL","reason":"x"}')(item)[0], "fail")
        self.assertEqual(llm_judge(lambda p, **k: "no json")(item)[0], "error")


class Narrator(unittest.TestCase):
    def test_number_extraction_skips_windows_and_years(self):
        got = [x for _, x, _ in numbers_in("In 2026 the 72 hours and 7-day totals hit 1.2M and 5.5% on day 3")]
        self.assertEqual(got, [1.2e6, 0.055, 3.0])

    def test_verify_catches_fabricated_number(self):
        c = connect(); seed(c)
        facts = build_facts(c, 1, range(2, 13))
        self.assertEqual(verify(render("[[f:velocity_72h]] views", facts), facts).unsupported, [])
        self.assertEqual(verify("Views grew 340% in 72 hours", facts).unsupported, ["340%"])
        self.assertEqual(verify("[[f:nope]]", facts).unknown_markers, ["nope"])

    def test_template_narrative_is_fully_grounded(self):
        c = connect(); seed(c)
        facts = build_facts(c, 1, range(2, 13))
        text = render(template_narrative("Iron Meridian", facts, c, 1), facts)
        self.assertEqual(verify(text, facts).groundedness, 1.0)
        self.assertIn("Primetime TV spot", text)


class Evals(unittest.TestCase):
    def test_harness_separates_grounded_from_hallucinating(self):
        c = connect(); seed(c)
        res = run_eval(c, default_cases(c), {"g": grounded_narrator, "b": inflating_baseline})
        self.assertEqual(res["g"]["pass_rate"], 1.0)
        self.assertEqual(res["g"]["hallucination_rate"], 0.0)
        self.assertEqual(res["b"]["pass_rate"], 0.0)
        self.assertGreater(res["b"]["hallucination_rate"], 0.5)

    def test_kappa(self):
        self.assertAlmostEqual(cohen_kappa([1, 1, 0, 0], [1, 0, 0, 0]), 0.5)
        self.assertEqual(cohen_kappa([1, 0, 1], [1, 0, 1]), 1.0)


if __name__ == "__main__":
    unittest.main()

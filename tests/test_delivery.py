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



class Annotate(unittest.TestCase):
    def test_annotate_always_agrees_with_verify_and_reconstructs_the_text(self):
        c = connect(); seed_all(c)
        items, _ = golden.make_items(c, 60)
        for it in items:
            facts = [Fact(**f) for f in it["facts"]]
            rep, seg = verify(it["text"], facts, ignore=it["ignore"]), annotate(it["text"], facts, ignore=it["ignore"])
            n = lambda s: sum(x["status"] == s for x in seg)
            self.assertEqual((n("unsupported"), n("misattributed"), n("style")), (len(rep.unsupported), len(rep.misattributed), len(rep.style_flags)), it["id"])
            self.assertEqual("".join(x["text"] for x in seg), render(it["text"], facts))

    def test_swapped_window_is_marked_on_the_right_number(self):
        c = connect(); seed_all(c)
        facts = build_facts(c, 1, range(2, 13)); v24 = next(f for f in facts if f.id == "velocity_24h")
        from groundcheck.narrator import fmt
        seg = annotate(f"{fmt(v24)} views in the first 72 hours.", facts)
        bad = [s for s in seg if s["status"] == "misattributed"]
        self.assertEqual([s["text"] for s in bad], [fmt(v24)]); self.assertIn("velocity_24h", bad[0]["detail"])


class FactsFile(unittest.TestCase):
    def _facts(self, data):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False); json.dump(data, f); f.close()
        self.addCleanup(os.unlink, f.name); return f.name

    def test_load_and_validate(self):
        ok = [{"id": "velocity_72h", "label": "views in first 72h", "value": 3480000, "unit": "views"}]
        self.assertEqual(load_facts(self._facts(ok))[0].value, 3480000.0)
        for bad in ([], [{"id": "x"}], [dict(ok[0], unit="bananas")], [dict(ok[0], value="3")], [ok[0], ok[0]]):
            with self.assertRaises(ValueError):
                load_facts(self._facts(bad))

    def test_cli_verify_exit_codes_on_any_facts_file(self):
        path = os.path.join(os.path.dirname(__file__), "..", "examples", "facts.example.json")
        good = "Aurora Rising: 3.48M views in the first 72 hours across 3 posts, with engagement of 8.49%."
        bad = "Aurora Rising: 1.99M views in the first 72 hours across 3 posts."
        for text, code in ((good, 0), (bad, 1), ("Views grew 340% in 72 hours.", 1)):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
                main(["verify", "--facts", path, "--text", text, "--ignore", "Aurora Rising"])
            self.assertEqual(cm.exception.code, code, buf.getvalue())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            main(["verify", "--facts", path, "--text", bad, "--ignore", "Aurora Rising"])
        self.assertIn("MISATTRIBUTED", buf.getvalue())


    def test_custom_cue_enables_wrong_metric_detection_and_uncued_facts_are_reported(self):
        from groundcheck.narrator import CUES
        from groundcheck.facts_io import uncued
        data = [{"id": "saves", "label": "saves", "value": 12400, "unit": "count", "cue": "saves"},
                {"id": "shares", "label": "shares", "value": 3100, "unit": "count", "cue": "shares"},
                {"id": "reach", "label": "reach", "value": 98000, "unit": "count"}]
        facts = load_facts(self._facts(data))
        self.addCleanup(lambda: [CUES.pop(k, None) for k in ("saves", "shares")])
        self.assertEqual(uncued(facts), ["reach"])
        self.assertEqual(len(verify("12.4K shares. 3.1K saves.", facts).misattributed), 2)    # values on each other's metric
        self.assertTrue(verify("12.4K saves. 3.1K shares.", facts).ok)
        self.assertEqual(verify("Reach hit 98K and 55K.", facts).unsupported, ["55K"])           # uncued: existence still enforced
        with self.assertRaises(ValueError):
            load_facts(self._facts([dict(data[0], id="bad", cue="(")]))
        self.assertNotIn("bad", CUES)                                                            # nothing registered from a failed file

    def test_cli_warns_when_facts_have_no_cue(self):
        path = os.path.join(tempfile.mkdtemp(), "f.json")
        with open(path, "w") as fh:
            json.dump([{"id": "reach", "label": "reach", "value": 98000, "unit": "count"}], fh)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
            main(["verify", "--facts", path, "--text", "Reach hit 98K."])
        self.assertEqual(cm.exception.code, 0); self.assertIn("no cue pattern for ['reach']", buf.getvalue())


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        conn = connect(check_same_thread=False); seed_all(conn)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(conn)))
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def call(self, p, data=None, method=None):
        req = urllib.request.Request(self.base + p, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def test_cases_facts_and_evals(self):
        s, cases = self.call("/api/cases/1"); self.assertEqual((s, [c["label"] for c in cases]), (200, ["clean", "wrong_claim_attribution", "number_wrong", "unsupported_claim", "causal_overclaim"]))
        self.assertTrue(self.call("/api/facts/1")[1]); self.assertEqual(self.call("/api/cases/999")[0], 404)
        ev = self.call("/api/evals")[1]
        self.assertEqual(ev["narrators"]["grounded"]["pass_rate"], 1.0); self.assertEqual(ev["narrators"]["swapped_window_baseline"]["pass_rate"], 0.0)
        self.assertEqual(ev["judge_flag_rate"]["clean"], 0.0)

    def test_verify_endpoint_marks_the_swapped_number_and_rejects_bad_input(self):
        swapped = next(c for c in self.call("/api/cases/1")[1] if c["label"] == "wrong_claim_attribution")["text"]
        s, r = self.call("/api/verify", json.dumps({"title_id": 1, "text": swapped}).encode())
        self.assertEqual(s, 200); self.assertFalse(r["ok"]); self.assertTrue(any(x["status"] == "misattributed" for x in r["segments"]))
        clean = next(c for c in self.call("/api/cases/1")[1] if c["label"] == "clean")["text"]
        self.assertTrue(self.call("/api/verify", json.dumps({"title_id": 1, "text": clean}).encode())[1]["ok"])
        for bad in (b"not json", b"{}", json.dumps({"title_id": "1", "text": "x"}).encode(), json.dumps({"title_id": 1, "text": "x" * 5000}).encode(), json.dumps({"title_id": True, "text": "x"}).encode()):
            self.assertEqual(self.call("/api/verify", bad)[0], 400)
        self.assertEqual(self.call("/api/verify", json.dumps({"title_id": 999, "text": "x"}).encode())[0], 404)
        self.assertEqual(self.call("/api/verify", b"x" * 30000)[0], 413)
        self.assertEqual(self.call("/api/titles", b"{}")[0], 404)            # POST is accepted only on /api/verify


class Browser(unittest.TestCase):
    def test_bench_highlights_the_wrong_number_in_a_real_browser(self):
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start(); browser = pw.chromium.launch()
        except Exception:
            self.skipTest("Playwright/Chromium not available")
        conn = connect(check_same_thread=False); seed_all(conn)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(App(conn))); threading.Thread(target=srv.serve_forever, daemon=True).start()
        errors = []
        try:
            page = browser.new_page(); page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{srv.server_address[1]}/"); page.wait_for_selector("#slate button"); page.locator("#slate button").first.click()
            page.wait_for_selector("mark.ok")
            self.assertIn("PASS", page.inner_text("#out"))
            page.get_by_role("button", name="Swapped window").click(); page.wait_for_selector("mark.misattributed")
            self.assertIn("FAIL", page.inner_text("#out")); self.assertIn("WRONG METRIC", page.inner_text("#out").upper())
            page.locator("#tabs button").nth(1).click(); page.wait_for_selector("text=Narrators scored")
            for bad in ("[object", "undefined", "NaN"):
                self.assertNotIn(bad, page.inner_text("main"))
        finally:
            browser.close(); pw.stop(); srv.shutdown(); srv.server_close()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()

"""Read-only dashboard server (stdlib only): JSON API + single-page UI.
Security posture: binds 127.0.0.1 by default, no auth (put it behind your own auth proxy before exposing it),
CSP + nosniff headers, request bodies capped, all user-derived text inserted with textContent (no innerHTML)."""
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import shutil
import tempfile

from .evals import default_cases, grounded_narrator, inflating_baseline, run_eval, swapped_window_baseline
from .golden import calibrate, case_variants, generate, rules_judge
from .metrics import build_facts
from .narrator import annotate, fmt, ignore_list, verify

MAX_BODY = 20_000
HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
           "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:"}


def _json(obj, status=200):
    return status, "application/json", json.dumps(obj, default=str).encode()

class App:
    def __init__(self, conn):
        self.conn, self.lock, self.cache = conn, threading.Lock(), {}

    def _cases(self):
        if "cases" not in self.cache:
            self.cache["cases"] = default_cases(self.conn)
        return self.cache["cases"]

    def meta(self):
        return {"sample_rows": int(self.conn.execute("SELECT COUNT(*) c FROM posts WHERE is_sample=1").fetchone()["c"])}

    def titles(self):
        return [{"id": r["id"], "name": r["name"], "genre": r["genre"]} for r in self.conn.execute("SELECT id, name, genre FROM titles ORDER BY id")]

    def facts(self, tid):
        case = next((c for c in self._cases() if c.title_id == tid), None)
        if not case:
            return None
        return [{"id": f.id, "label": f.label, "value": f.value, "text": fmt(f), "query": f.query} for f in build_facts(self.conn, tid, case.peer_ids)]

    def cases(self, tid):
        case = next((c for c in self._cases() if c.title_id == tid), None)
        if not case:
            return None
        return [{"label": it["_injected"] or "clean", "text": it["text"]} for it in case_variants(self.conn, case)]

    def verify_text(self, body):
        try:
            d = json.loads(body or b"{}")
        except ValueError:
            return _json({"error": "invalid JSON"}, 400)
        tid, text = d.get("title_id") if isinstance(d, dict) else None, d.get("text") if isinstance(d, dict) else None
        if not isinstance(tid, int) or isinstance(tid, bool) or not isinstance(text, str) or not text.strip() or len(text) > 4000:
            return _json({"error": "title_id (int) and text (1-4000 chars) are required"}, 400)
        case = next((c for c in self._cases() if c.title_id == tid), None)
        if not case:
            return _json({"error": "unknown title"}, 404)
        facts = build_facts(self.conn, tid, case.peer_ids)
        ign = ignore_list(self.conn, tid, case.name)
        rep = verify(text, facts, ignore=ign)
        return _json({"ok": rep.ok, "numbers": rep.n_numbers, "claim_accuracy": rep.claim_accuracy, "problems": rep.reasons(),
                      "segments": annotate(text, facts, ignore=ign)})

    def evals(self):
        if "ev" not in self.cache:
            res = run_eval(self.conn, self._cases(), {"grounded": grounded_narrator, "swapped_window_baseline": swapped_window_baseline, "inflating_baseline": inflating_baseline})
            d = tempfile.mkdtemp()
            try:
                generate(self.conn, d, n=60)
                flag = calibrate(d, {"rules": rules_judge})["judges"]["rules"]["injected_defect_flag_rate"]
            finally:
                shutil.rmtree(d, ignore_errors=True)
            defects = [v for k, v in flag.items() if k != "clean"]
            self.cache["ev"] = {"narrators": res, "judge_flag_rate": flag, "n_items": 60, "headline": [
                {"label": "Grounded summaries passing", "value": res["grounded"]["pass_rate"]},
                {"label": "Swapped windows caught", "value": 1 - res["swapped_window_baseline"]["pass_rate"]},
                {"label": "Injected defects flagged (regression check)", "value": sum(defects) / len(defects) if defects else 0.0},
                {"label": "False alarms on clean", "value": flag.get("clean", 0.0)}]}
        return self.cache["ev"]

    def route(self, method, raw_path, body):
        p = urlsplit(raw_path).path
        with self.lock:
            if method == "POST":
                return self.verify_text(body) if p == "/api/verify" else _json({"error": "not found"}, 404)
            if method != "GET":
                return _json({"error": "method not allowed"}, 405)
            if p == "/":
                return 200, "text/html; charset=utf-8", PAGE.encode()
            if p == "/healthz":
                return _json({"ok": True})
            if p == "/api/meta":
                return _json(self.meta())
            if p == "/api/titles":
                return _json(self.titles())
            if p == "/api/evals":
                return _json(self.evals())
            m = re.fullmatch(r"/api/(cases|facts)/(\d+)", p)
            if m:
                out = self.cases(int(m[2])) if m[1] == "cases" else self.facts(int(m[2]))
                return _json(out) if out is not None else _json({"error": "not found"}, 404)
        return _json({"error": "not found"}, 404)


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def _run(self, method):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    status, ctype, body = _json({"error": "payload too large"}, 413)
                else:
                    status, ctype, body = app.route(method, self.path, self.rfile.read(n) if n else b"")
            except Exception as exc:  # never leak a stack trace to the client
                print(f"error on {self.path}: {exc!r}", file=sys.stderr)
                status, ctype, body = _json({"error": "internal error"}, 500)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._run("GET")

        def do_POST(self):
            self._run("POST")

        def log_message(self, fmt_, *args):
            pass
    return Handler


def serve(conn, host="127.0.0.1", port=8000):
    srv = ThreadingHTTPServer((host, port), make_handler(App(conn)))
    print(f"dashboard on http://{host}:{port}  (read-only, no auth)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.server_close()


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>GroundCheck - Claim Bench</title><style>
:root{--paper:#f2ead8;--panel:#ebe2cb;--ink:#1c1a16;--muted:#6b6455;--rule:#cfc4a8;--accent:#c2410c;--cool:#1f4e5f;--good:#2f6b3a;--bad:#a4261b;
--serif:"Iowan Old Style","Palatino Linotype","Book Antiqua",Georgia,serif;--mono:ui-monospace,"SF Mono","Cascadia Mono",Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--paper:#15130f;--panel:#1d1a14;--ink:#ece3cc;--muted:#a39a84;--rule:#3a3528;--accent:#f08a4b;--cool:#7fc0d4;--good:#7fc98a;--bad:#f08478}}
*{box-sizing:border-box}html,body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 var(--serif)}
.mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
.banner{background:var(--accent);color:var(--paper);font:600 12px var(--mono);letter-spacing:.08em;text-transform:uppercase;padding:6px 16px;text-align:center}
.wrap{display:grid;grid-template-columns:270px 1fr;min-height:100vh}
aside{border-right:1px solid var(--rule);padding:22px 0}
aside h2{font:600 11px var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin:0 20px 10px}
.slate button{all:unset;display:flex;justify-content:space-between;gap:8px;width:100%;box-sizing:border-box;padding:9px 20px;cursor:pointer;border-left:3px solid transparent;border-bottom:1px solid var(--rule)}
.slate button:hover{background:var(--panel)}.slate button[aria-current=true]{border-left-color:var(--accent);background:var(--panel)}
.slate small{display:block;color:var(--muted);font:11px var(--mono);text-transform:uppercase;letter-spacing:.08em}
main{padding:26px 34px 60px;min-width:0}
.kicker{font:600 11px var(--mono);letter-spacing:.16em;text-transform:uppercase;color:var(--accent)}
h1{font:700 clamp(30px,4.4vw,52px)/1.02 var(--serif);letter-spacing:-.02em;margin:6px 0 18px}
.figs{display:grid;grid-template-columns:repeat(4,1fr);border-top:2px solid var(--ink);border-bottom:1px solid var(--rule);margin-bottom:22px}
.fig{padding:12px 16px;border-right:1px solid var(--rule)}.fig:last-child{border-right:0}
.fig b{display:block;font:600 clamp(20px,2.6vw,32px) var(--mono)}.fig span{font:11px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
nav.tabs{display:flex;gap:22px;border-bottom:1px solid var(--rule);margin-bottom:22px}
nav.tabs button{all:unset;cursor:pointer;padding:8px 0;font:600 12px var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--muted);border-bottom:2px solid transparent}
nav.tabs button[aria-selected=true]{color:var(--ink);border-bottom-color:var(--accent)}
.read{font-size:19px;max-width:64ch;margin:0 0 10px}
.badge{display:inline-block;font:600 11px var(--mono);letter-spacing:.06em;padding:3px 8px;border:1px solid currentColor;margin-bottom:18px}.ok{color:var(--good)}.fail{color:var(--bad)}
h3{font:600 11px var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin:26px 0 8px;border-bottom:1px solid var(--rule);padding-bottom:6px}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:7px 10px 7px 0;border-bottom:1px solid var(--rule);vertical-align:top}
th{font:600 11px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}td.n,th.n{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums;padding-left:18px}
.dim{color:var(--muted)}.chip{font:600 11px var(--mono);padding:2px 6px;border:1px solid currentColor;text-transform:uppercase}
.bar{display:inline-block;height:8px;background:var(--cool);vertical-align:middle}.bar.neg{background:var(--accent)}
.call{border-left:3px solid var(--accent);background:var(--panel);padding:10px 14px;margin:8px 0}
svg text{font:11px var(--mono);fill:var(--muted)}.scroll{overflow-x:auto}
@media (max-width:820px){.wrap{grid-template-columns:1fr}aside{border-right:0;border-bottom:1px solid var(--rule);padding:12px 0 0}.slate{display:flex;overflow-x:auto}.slate button{flex:0 0 auto;width:auto;border-left:0;border-bottom:3px solid transparent}.slate button[aria-current=true]{border-bottom-color:var(--accent)}.figs{grid-template-columns:1fr 1fr}main{padding:20px 16px}}

.field{display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin:0 0 18px}
select,textarea,button.act{font:14px var(--serif);color:var(--ink);background:var(--panel);border:1px solid var(--rule);padding:6px 10px}
textarea{width:100%;min-height:110px;resize:vertical;line-height:1.5}
button.act{cursor:pointer;font:600 12px var(--mono);letter-spacing:.08em;text-transform:uppercase;background:var(--ink);color:var(--paper);border-color:var(--ink)}
label.f{font:600 11px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--muted);display:flex;flex-direction:column;gap:4px}
mark{background:none;color:inherit;padding:0 3px;border-bottom:2px solid}
mark.ok{border-color:var(--good)}mark.unsupported{border-color:var(--bad);background:color-mix(in srgb,var(--bad) 14%,transparent)}
mark.misattributed{border-color:var(--accent);background:color-mix(in srgb,var(--accent) 16%,transparent)}mark.style{border-color:var(--muted);border-bottom-style:dashed}
.legend{display:flex;gap:16px;flex-wrap:wrap;font:11px var(--mono);color:var(--muted);margin:8px 0 0}
.two{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(0,1fr);gap:28px}@media (max-width:980px){.two{grid-template-columns:1fr}}
.pill{font:600 10px var(--mono);letter-spacing:.08em;text-transform:uppercase;padding:2px 6px;border:1px solid currentColor;margin-left:6px}
tr.sel td{background:var(--panel)}tr.click{cursor:pointer}tr.click:hover td{background:var(--panel)}
.wrap.single{grid-template-columns:1fr}
@media (max-width:820px){.wrap,.wrap.single{grid-template-columns:minmax(0,1fr)}aside,main{min-width:0}.slate{max-width:100vw}.figs{grid-template-columns:1fr 1fr}.fig{border-right:0;border-bottom:1px solid var(--rule)}}main{min-width:0}svg{max-width:100%;height:auto}@media (max-width:820px){th,td{padding:6px 6px 6px 0;font-size:13px}td.n,th.n{padding-left:6px}.bar2{max-width:44px}.chip{font-size:10px}}button.act.off{background:var(--panel);color:var(--ink);border-color:var(--rule)}.bar2{display:inline-block;height:10px;background:var(--cool);vertical-align:middle}.bar2.warn{background:var(--accent)}</style></head><body><div id="banner" class="banner" hidden>Synthetic demo data. Not real platform data.</div><div class="wrap"><aside><h2>Titles under test</h2><div id="slate" class="slate"></div></aside><main><div class="kicker">Claim bench</div><h1 id="title">Is every number on the right claim?</h1><div id="figs" class="figs"></div><nav class="tabs" role="tablist" id="tabs"></nav><section id="panel"></section></main></div><script>
"use strict";
const $=s=>document.querySelector(s);
function h(tag,attrs,...kids){const e=document.createElement(tag);for(const[k,v]of Object.entries(attrs||{})){if(k==="class")e.className=v;else if(k.startsWith("on"))e.addEventListener(k.slice(2),v);else e.setAttribute(k,v)}
 for(const c of kids.flat(Infinity)){if(c==null||c===false)continue;e.append(c.nodeType?c:document.createTextNode(String(c)))}return e}
function sv(tag,attrs,...kids){const e=document.createElementNS("http://www.w3.org/2000/svg",tag);for(const[k,v]of Object.entries(attrs||{}))e.setAttribute(k,v);for(const c of kids.flat(Infinity))if(c!=null)e.append(typeof c==="string"?document.createTextNode(c):c);return e}
const api=async p=>{const r=await fetch(p);if(!r.ok)throw new Error(p+" "+r.status);return r.json()};
const pct=(x,d=0)=>x==null?"n/a":(x*100).toFixed(d)+"%";const big=v=>v>=1e6?(v/1e6).toFixed(2)+"M":v>=1e3?(v/1e3).toFixed(0)+"K":Math.round(v)+"";


const state={titles:[],cur:null,cases:[],facts:[],idx:0,tab:"bench",ev:null};
const LABEL={clean:"Clean summary",wrong_claim_attribution:"Swapped window",number_wrong:"Inflated number",unsupported_claim:"Invented trend",causal_overclaim:"Causal overclaim"};
const TAG={unsupported:"no source",misattributed:"wrong metric"};
const TABS=[["bench","Claim bench"],["evals","Evals"]];
async function verifyText(text){const r=await fetch("/api/verify",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({title_id:state.cur.id,text})});if(!r.ok)throw new Error("verify failed ("+r.status+")");return r.json()}
function result(r){return h("div",{},
 h("span",{class:"badge "+(r.ok?"ok":"fail")},r.ok?`PASS: ${r.numbers} numbers, each on the right claim`:`FAIL: ${r.problems.length} problem${r.problems.length>1?"s":""}`),
 h("p",{class:"read"},r.segments.map(s=>s.status==="plain"?s.text:[h("mark",{class:s.status,title:s.detail},s.text),(s.status==="ok"?null:h("span",{class:"pill"},TAG[s.status]||s.detail))])),
 h("div",{class:"legend"},h("span",{},"green underline: number matches its claim"),h("span",{},"orange: real number, wrong metric or window"),h("span",{},"red: no computed fact has this value"),h("span",{},"dashed: hype or unproven causation")),
 r.problems.map(p=>h("div",{class:"call"},p)))}
function factsTable(){return h("table",{},h("thead",{},h("tr",{},h("th",{},"Fact"),h("th",{class:"n"},"Value"))),h("tbody",{},state.facts.map(f=>h("tr",{},h("td",{},f.label,h("div",{class:"dim mono",style:"font-size:11px"},f.id)),h("td",{class:"n"},f.text)))))}
function bench(){const c=state.cases[state.idx];const ta=h("textarea",{id:"txt",spellcheck:"false"});ta.value=c.text;const out=h("div",{id:"out",style:"margin-top:14px"});
 const run=async()=>{try{out.replaceChildren(result(await verifyText(ta.value)))}catch(e){out.replaceChildren(h("div",{class:"call fail"},e.message))}};setTimeout(run,0);
 return h("div",{class:"two"},h("div",{},
  h("div",{class:"field"},state.cases.map((x,i)=>h("button",{class:"act"+(i===state.idx?"":" off"),onclick:()=>{state.idx=i;show()}},LABEL[x.label]||x.label))),
  h("label",{class:"f"},"Summary under test (edit it, then re-verify)",ta),h("div",{class:"field",style:"margin-top:10px"},h("button",{class:"act",onclick:run},"Verify claims")),out),
  h("div",{},h("h3",{style:"margin-top:0"},"Ground truth for this title"),factsTable()))}
const pc=x=>(x*100).toFixed(0)+"%";
function evalsPanel(){const e=state.ev;if(!e)return h("p",{class:"dim"},"Loading evals");const N=e.narrators;
 const rows=Object.entries(N).map(([k,v])=>h("tr",{},h("td",{},k.replace(/_/g," ")),h("td",{class:"n"},pc(v.numeric_recall)),h("td",{class:"n"},pc(v.hallucination_rate)),h("td",{class:"n"},pc(v.misattribution_rate)),h("td",{class:"n"},v.style_flags),h("td",{class:"n"},pc(v.pass_rate))));
 const fr=Object.entries(e.judge_flag_rate).sort((a,b)=>(a[0]==="clean")-(b[0]==="clean"));
 return h("div",{},h("h3",{style:"margin-top:0"},"Narrators scored on "+N.grounded.cases+" cases"),
  h("div",{class:"scroll"},h("table",{},h("thead",{},h("tr",{},["Narrator","Numeric recall","Invented numbers","Wrong-metric numbers","Style flags","Pass rate"].map((x,i)=>h("th",{class:i?"n":""},x)))),h("tbody",{},rows))),
  h("p",{class:"dim"},"The swapped-window baseline has no invented numbers: an existence-only check would pass it. Claim-level checking is what fails it."),
  h("h3",{},`Rules judge on ${e.n_items} generated summaries with injected defects (needs no human labels)`),
  h("table",{},h("thead",{},h("tr",{},h("th",{},"Injected defect"),h("th",{},"Flagged"),h("th",{class:"n"},"Rate"))),
   h("tbody",{},fr.map(([k,v])=>h("tr",{},h("td",{},k==="clean"?"none (false-alarm rate)":k.replace(/_/g," ")),h("td",{},h("span",{class:"bar2"+(k==="clean"?" warn":""),style:`width:${Math.max(2,v*220)}px`})),h("td",{class:"n"},pc(v)))))),
  h("div",{class:"call"},h("b",{},"Read this before quoting these numbers. "),"The injected defects are exactly the classes the rules were written to catch, so near-100% here is a regression check, not evidence of general accuracy. Real accuracy is agreement with YOUR labels (Cohen's kappa), measured with the golden-set workflow. No human labels are bundled or faked."))}
function show(){$("#panel").replaceChildren(state.tab==="bench"?(state.cases.length?bench():h("p",{class:"dim"},"Pick a title")):evalsPanel())}
async function loadTitle(t){state.cur=t;state.idx=0;document.querySelectorAll("#slate button").forEach(b=>b.setAttribute("aria-current",b.dataset.id==t.id));
 try{const [c,f]=await Promise.all([api("/api/cases/"+t.id),api("/api/facts/"+t.id)]);state.cases=c;state.facts=f;show()}catch(e){$("#panel").replaceChildren(h("div",{class:"call fail"},"Could not load: "+e.message))}}
(async()=>{const m=await api("/api/meta");if(m.sample_rows)$("#banner").hidden=false;state.titles=await api("/api/titles");
 $("#slate").replaceChildren(...state.titles.map(t=>h("button",{"data-id":t.id,onclick:()=>loadTitle(t)},h("span",{},t.name,h("small",{},t.genre||"")))));
 $("#tabs").replaceChildren(...TABS.map(([k,l])=>h("button",{role:"tab","aria-selected":k===state.tab,onclick:e=>{state.tab=k;document.querySelectorAll("#tabs button").forEach(b=>b.setAttribute("aria-selected",b===e.currentTarget));show()}},l)));
 api("/api/evals").then(ev=>{state.ev=ev;$("#figs").replaceChildren(...ev.headline.map(x=>h("div",{class:"fig"},h("b",{},pc(x.value)),h("span",{},x.label))));if(state.tab==="evals")show()});
 if(state.titles.length)loadTitle(state.titles[0])})();

</script></body></html>"""

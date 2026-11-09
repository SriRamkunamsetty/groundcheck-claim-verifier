"""Load a facts file so the verifier works on ANY analytics, not just the bundled demo data.
Format: JSON list of {"id", "label", "value", "unit": views|ratio|pct|count, "cue": optional regex}.
The cue names the words that signal this metric in prose (e.g. "saves?"). It enables wrong-metric detection for
that fact and is registered process-wide. Facts without a cue (and without a built-in one) are still checked for
existence, hype and unproven causation, but not for being attached to the right metric."""
import json
import re

from .metrics import Fact
from .narrator import CUES, _cue

UNITS = ("views", "ratio", "pct", "count")


def load_facts(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list) or not data:
        raise ValueError("facts file must be a non-empty JSON list")
    out, seen, cues = [], set(), {}
    for i, d in enumerate(data):
        if not isinstance(d, dict) or not {"id", "label", "value", "unit"} <= set(d):
            raise ValueError(f"fact #{i} needs id, label, value, unit")
        if d["unit"] not in UNITS:
            raise ValueError(f"fact '{d['id']}': unit must be one of {UNITS}")
        if not isinstance(d["value"], (int, float)) or isinstance(d["value"], bool):
            raise ValueError(f"fact '{d['id']}': value must be a number")
        if d["id"] in seen:
            raise ValueError(f"duplicate fact id '{d['id']}'")
        seen.add(d["id"])
        if d.get("cue") is not None:
            if not isinstance(d["cue"], str) or not d["cue"]:
                raise ValueError(f"fact '{d['id']}': cue must be a non-empty regex string")
            try:
                re.compile(d["cue"])
            except re.error as exc:
                raise ValueError(f"fact '{d['id']}': bad cue regex: {exc}")
            cues[d["id"]] = d["cue"]
        out.append(Fact(d["id"], d["label"], float(d["value"]), d["unit"], d.get("query", "supplied by you")))
    CUES.update(cues)          # only after the whole file validated
    return out


def uncued(facts):
    """Fact ids with no cue pattern: numbers are checked for existence but not for the right metric."""
    return [f.id for f in facts if _cue(f.id) is None]

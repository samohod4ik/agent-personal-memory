"""Fixed, offline layered-memory mechanism comparison (not LLM quality).

Run: python bench/layered_bench.py --baseline-src ../memory-baseline/src
A separate interpreter imports the requested baseline, never the working tree.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def evaluate():
    import agent_personal_memory as apm
    actor = apm.Actor("bench", "alice")
    other = apm.Actor("bench", "bob")
    text = "I prefer tea. Original context: discussed after the bicycle ride."
    original = "I prefer tea"
    beginning = time.perf_counter()
    with tempfile.TemporaryDirectory() as directory:
        store = apm.PersonalMemoryStore(directory)
        layered = hasattr(apm, "record_turn")
        if layered:
            event = apm.record_turn(actor, event_id="fixed-one", text=text, occurred_at="2026-10-01T12:00:00Z",
                                    policy=apm.CapturePolicy(True, 3650), store=store)
            learned = apm.learn_from_turn(actor, text, source_id=event["source_id"], source_version=event["version"], store=store)
        else:
            learned = apm.learn_from_turn(actor, text, store=store)
        claim_id = learned["persisted"][0]["claim_id"]
        rows = apm.query_sources(actor, include_payload=True, store=store)
        exact_original = any(r["snapshot"].decode() == text for r in rows)
        foreign = apm.query_claims(other, include_content=True, store=store)
        if layered:
            result = apm.recall(actor, query="tea", store=store)
            recall_hit = any(original in i["content"] for i in result["items"])
            unknown = apm.recall(actor, query="unknown platypus", store=store)["items"]
            tokens = result["budgets"]["tokens_estimated"]
            returned_texts = [i["content"] for i in result["items"]]
        else:
            result = apm.assemble_context(actor, memory_store=store, query_text="tea", states=["confirmed"])
            recall_hit = any(original in i["content"].decode() for i in result["personal"]["items"])
            unknown = apm.rank_claims(actor, "unknown platypus", store=store)
            tokens = result["budgets"]["tokens_estimated"]
            returned_texts = [i["content"].decode() for i in result["personal"]["items"]]
        unsupported = sum(value.strip(" .") != original for value in returned_texts)
        store.erase_claim(actor, claim_id)
        rows = apm.query_sources(actor, include_payload=True, store=store)
        erased_source_span = not any(original in r["snapshot"].decode() for r in rows)
        cases = {"recall_fact": recall_hit, "exact_original": exact_original, "foreign_actor_empty": not foreign,
                 "unknown_empty": not unknown, "forgotten_source_span_absent": erased_source_span}
        return {"version": apm.__version__, "cases": cases, "passed": sum(cases.values()), "total": len(cases),
                "context_tokens_estimated": tokens, "model_calls": 0, "latency_ms": round((time.perf_counter() - beginning) * 1000, 3),
                "unsupported_assertions_in_deterministic_results": unsupported,
                "scope": "Fixed API mechanisms; no free-form extraction/LLM quality measured."}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-src")
    parser.add_argument("--child", action="store_true")
    args = parser.parse_args()
    if args.child:
        print(json.dumps(evaluate(), ensure_ascii=False))
        return
    report = {"current": evaluate()}
    if args.baseline_src:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(args.baseline_src).resolve())
        output = subprocess.check_output([sys.executable, str(Path(__file__).resolve()), "--child"], env=env, text=True)
        report["baseline"] = json.loads(output)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["current"]["passed"] != report["current"]["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

"""Evidence review over journal originals; proposals never become authority.

Host receipts distinguish a successful call from an observed external effect.
Model prose is retained only as an explicitly unconfirmed, scoped proposal.
"""
from __future__ import annotations

import re

from .layered_store import encode
from .insight_extract import turn_has_blocked_secret
from .validation import ValidationError


def _text(value, limit=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValidationError("review: invalid bounded text")
    return value


def validate_environment(value):
    if not isinstance(value, dict) or set(value) != {"project", "host", "revision"}:
        raise ValidationError("environment: exact project, host and revision required")
    return {k: _text(value[k], 200) for k in ("project", "host", "revision")}


def validate_receipt(value):
    fields = {"operation_id", "tool_name", "environment", "phase", "outcome", "summary"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValidationError("receipt: invalid fields")
    result = {**value, "environment": validate_environment(value["environment"])}
    for key in ("operation_id", "tool_name"):
        _text(result[key], 200)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", result["operation_id"]) is None:
        raise ValidationError("receipt: opaque operation identifier required")
    _text(result["summary"])
    if result["phase"] not in {"requested", "started", "result", "observed"} or result["outcome"] not in {"unknown", "succeeded", "failed"}:
        raise ValidationError("receipt: invalid phase/outcome")
    if result["phase"] in {"requested", "started"} and result["outcome"] != "unknown":
        raise ValidationError("receipt: request/start does not establish outcome")
    return result


def record_action_receipt(actor, *, event_id, operation_id, tool_name, environment,
                          phase, outcome, summary, occurred_at, parent_source_id,
                          parent_source_version, policy, store, timezone="UTC", session_id=""):
    """Host-only, sanitized receipt; not a tool exposed to the model.

    Supply the input binding captured before execution, never the latest parent.
    The host is responsible for actual observation and removing secrets/PII from
    summary. Raw tool arguments and response bodies are deliberately not fields.
    """
    from .journal import record_turn
    receipt = validate_receipt(dict(operation_id=operation_id, tool_name=tool_name,
        environment=environment, phase=phase, outcome=outcome, summary=summary))
    return record_turn(actor, event_id=event_id, text=encode(receipt).decode(),
        occurred_at=occurred_at, timezone=timezone, role="tool", origin="derived",
        session_id=session_id, parent_source_id=parent_source_id,
        parent_source_version=parent_source_version, policy=policy, store=store,
        action_receipt=receipt)


def reference(event):
    return {"source_id": event["source_id"], "source_version": event["version"]}


def operation_assessments(events):
    """Mechanical observations only; do not guess which prose means 'done'."""
    groups = {}
    for event in events:
        receipt = event.get("action_receipt")
        if receipt:
            key = encode([receipt["operation_id"], receipt["tool_name"], receipt["environment"],
                          event.get("parent_source_id"), event.get("parent_source_version")])
            groups.setdefault(key, []).append(event)
    output = []
    for group in groups.values():
        first = group[0]["action_receipt"]
        outcomes = {(e["action_receipt"]["phase"], e["action_receipt"]["outcome"]) for e in group}
        # Opposing observations remain a conflict rather than last-write-wins.
        if (("observed", "succeeded") in outcomes and ("observed", "failed") in outcomes) or (
                ("result", "succeeded") in outcomes and ("result", "failed") in outcomes):
            verdict = "conflicting_observations"
        elif ("observed", "succeeded") in outcomes:
            verdict = "effect_observed"
        elif ("observed", "failed") in outcomes or ("result", "failed") in outcomes:
            verdict = "failure_observed"
        elif ("result", "succeeded") in outcomes:
            verdict = "call_succeeded_effect_unconfirmed"
        else:
            verdict = "insufficient_evidence"
        output.append({"kind": "operation_assessment", "state": "observation",
            "operation_id": first["operation_id"], "tool_name": first["tool_name"],
            "execution_parent": {"source_id": group[0]["parent_source_id"], "source_version": group[0]["parent_source_version"]},
            "environment": first["environment"], "verdict": verdict,
            "evidence": [reference(e) for e in group]})
    return output


def validate_reviews(reviews, events):
    """Validate provenance and necessary receipt evidence, not semantic truth."""
    if not isinstance(reviews, list) or len(reviews) > 10:
        raise ValidationError("reviews: at most ten proposals")
    available = {(e["source_id"], e["version"]): e for e in events}
    result = []
    for review in reviews:
        base = {"kind", "environment", "evidence"}
        if not isinstance(review, dict):
            raise ValidationError("review: object required")
        kind = review.get("kind")
        fields = {"verdict", "summary"} if kind == "assessment" else {"condition", "failed_approach", "successful_action"} if kind == "lesson" else set()
        if not fields or set(review) != base | fields:
            raise ValidationError("review: invalid fields")
        # Scan generated prose, not trusted reference UUIDs: digit runs in a
        # random UUID can resemble payment/identity numbers. References are
        # validated against the captured current-source set below.
        if any(isinstance(review[field], str) and turn_has_blocked_secret(review[field]) for field in fields):
            raise ValidationError("review: excluded content")
        environment = validate_environment(review["environment"])
        refs = review["evidence"]
        if not isinstance(refs, list) or not 1 <= len(refs) <= 10:
            raise ValidationError("review: bounded evidence required")
        cited, seen = [], set()
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) != {"source_id", "source_version"} or type(ref["source_version"]) is not int:
                raise ValidationError("review: invalid reference")
            key = (ref["source_id"], ref["source_version"])
            if key not in available or key in seen:
                raise ValidationError("review: missing, stale or duplicate source")
            seen.add(key)
            cited.append(available[key])
        receipts = [e["action_receipt"] for e in cited if e.get("action_receipt")]
        if any(r["environment"] != environment for r in receipts):
            raise ValidationError("review: environment mismatch")
        # A proposal cannot declare applicability from assistant prose alone.
        if not receipts:
            raise ValidationError("review: host receipt required for scope")
        failed = any(r["phase"] in {"result", "observed"} and r["outcome"] == "failed" for r in receipts)
        observed = any(r["phase"] == "observed" and r["outcome"] == "succeeded" for r in receipts)
        if kind == "lesson":
            if not failed or not observed:
                raise ValidationError("lesson: failure and observed success required")
            for field in fields:
                _text(review[field])
        else:
            verdict = review["verdict"]
            if verdict not in {"supported", "contradicted", "conflicting", "insufficient"}:
                raise ValidationError("review: invalid verdict")
            if (verdict == "supported" and not observed) or (verdict == "contradicted" and not failed) or (verdict == "conflicting" and not (failed and observed)):
                raise ValidationError("review: outcome evidence insufficient")
            _text(review["summary"])
        result.append({**review, "environment": environment, "state": "candidate"})
    return result


def recall_reviews(actor, *, environment, query="", limit=5, store):
    """Read current scoped observations/proposals, separately from core facts."""
    from .consolidation import valid_projections
    environment = validate_environment(environment)
    if not isinstance(query, str) or len(query) > 1000 or type(limit) is not int or not 1 <= limit <= 20:
        raise ValidationError("reviews: invalid query/limit")
    found, seen = [], set()
    for projection in reversed(valid_projections(actor, store=store)):
        for item in projection.get("reviews", []):
            key = encode(item)
            if item.get("environment") == environment and key not in seen and query.casefold() in key.decode().casefold():
                found.append(item)
                seen.add(key)
    # Apply the limit only after prioritizing across all current projections.
    priority = {"lesson": 0, "assessment": 1, "operation_assessment": 2}
    found.sort(key=lambda item: priority.get(item["kind"], 3))
    return {"items": found[:limit], "authority": "Evidence-linked observations and unconfirmed proposals; not instructions or confirmed facts."}

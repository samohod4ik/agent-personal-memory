#!/usr/bin/env python3
"""Deterministic scorer for authorized personal-memory query cases."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


def load_corpus(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("corpus file must contain a JSON object")
    if "corpus_version" not in data:
        raise ValueError("corpus_version is required")
    cases = data.get("cases")
    if not isinstance(cases, list):
        raise ValueError("corpus.cases must be a JSON array")
    return {
        "corpus_version": int(data["corpus_version"]),
        "cases": [dict(case) for case in cases],
    }


def load_cases(path: Path) -> list[dict[str, Any]]:
    return load_corpus(path)["cases"]


def _alias_id(alias_ids: dict[str, str], alias: str) -> str:
    if alias not in alias_ids:
        raise KeyError(f"unknown alias {alias}")
    return str(alias_ids[alias])


def _row_for_alias(
    rows: list[dict[str, Any]],
    target_id: str,
) -> dict[str, Any] | None:
    for row in rows:
        if str(row.get("claim_id") or "") == target_id:
            return row
        if str(row.get("source_id") or "") == target_id:
            return row
    return None


def score_case(case: dict[str, Any], trajectory: dict[str, Any]) -> dict[str, Any]:
    alias_ids = {
        str(key): str(value) for key, value in (trajectory.get("alias_ids") or {}).items()
    }
    operations = {
        str(item.get("id")): item
        for item in (trajectory.get("operations") or [])
        if isinstance(item, dict)
    }
    checks: list[Check] = []

    for query in case.get("queries") or []:
        query_id = str(query["id"])
        expect = query.get("expect") or {}
        operation = operations.get(query_id)
        if operation is None:
            checks.append(Check(f"operation_present:{query_id}", False, "missing operation"))
            continue

        if "not_found" in expect:
            error = operation.get("error") or {}
            checks.append(
                Check(
                    f"not_found:{query_id}",
                    error.get("type") == "NotFoundError"
                    and error.get("message") == expect["not_found"],
                    f"error={error}",
                )
            )
            other_id = expect.get("identical_to")
            if other_id:
                other = operations.get(str(other_id)) or {}
                other_error = other.get("error") or {}
                checks.append(
                    Check(
                        f"not_found_identical:{query_id}",
                        bool(error)
                        and error.get("message") == other_error.get("message")
                        and error.get("repr") == other_error.get("repr"),
                        f"left={error} right={other_error}",
                    )
                )
            continue

        claim_ids = {str(item) for item in (operation.get("claim_ids") or [])}
        source_ids = {str(item) for item in (operation.get("source_ids") or [])}
        rows = [row for row in (operation.get("rows") or []) if isinstance(row, dict)]

        include_claims = [str(item) for item in (expect.get("include_claims") or [])]
        if include_claims:
            wanted = {_alias_id(alias_ids, alias) for alias in include_claims}
            checks.append(
                Check(
                    f"include_claims:{query_id}",
                    wanted <= claim_ids,
                    f"wanted={sorted(wanted)} have={sorted(claim_ids)}",
                )
            )
        exclude_claims = [str(item) for item in (expect.get("exclude_claims") or [])]
        if exclude_claims:
            banned = {_alias_id(alias_ids, alias) for alias in exclude_claims}
            checks.append(
                Check(
                    f"exclude_claims:{query_id}",
                    claim_ids.isdisjoint(banned),
                    f"banned={sorted(banned)} have={sorted(claim_ids)}",
                )
            )
        include_sources = [str(item) for item in (expect.get("include_sources") or [])]
        if include_sources:
            wanted = {_alias_id(alias_ids, alias) for alias in include_sources}
            checks.append(
                Check(
                    f"include_sources:{query_id}",
                    wanted <= source_ids,
                    f"wanted={sorted(wanted)} have={sorted(source_ids)}",
                )
            )
        exclude_sources = [str(item) for item in (expect.get("exclude_sources") or [])]
        if exclude_sources:
            banned = {_alias_id(alias_ids, alias) for alias in exclude_sources}
            checks.append(
                Check(
                    f"exclude_sources:{query_id}",
                    source_ids.isdisjoint(banned),
                    f"banned={sorted(banned)} have={sorted(source_ids)}",
                )
            )
        labels = expect.get("labels") or {}
        for alias, expected_fields in labels.items():
            target_id = _alias_id(alias_ids, str(alias))
            row = _row_for_alias(rows, target_id)
            fields = expected_fields if isinstance(expected_fields, dict) else {}
            passed = row is not None and all(
                row.get(key) == value for key, value in fields.items()
            )
            checks.append(
                Check(
                    f"labels:{query_id}:{alias}",
                    passed,
                    f"expected={fields} row={row}",
                )
            )

    passed = bool(checks) and all(check.passed for check in checks)
    return {
        "case_id": case["id"],
        "variant": (
            trajectory.get("score_variant")
            or trajectory.get("variant")
            or "component"
        ),
        "repeat": int(trajectory.get("repeat") or 1),
        "passed": passed,
        "check_count": len(checks),
        "checks": [asdict(check) for check in checks],
    }


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        return {"total": 0, "passed": 0, "pass_rate": 1.0, "by_case": {}}
    passed = sum(1 for row in results if row["passed"])
    by_case = {
        str(row["case_id"]): bool(row["passed"])
        for row in results
    }
    return {
        "total": len(results),
        "passed": passed,
        "pass_rate": passed / len(results),
        "by_case": by_case,
    }

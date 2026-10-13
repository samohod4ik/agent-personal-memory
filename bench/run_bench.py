#!/usr/bin/env python3
"""Run deterministic authorized-query cases against agent_personal_memory."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from agent_personal_memory.query import (  # noqa: E402
    get_authorized_claim,
    get_authorized_source,
    query_claims,
    query_sources,
)
from agent_personal_memory.store import (  # noqa: E402
    NotFoundError,
    PersonalMemoryStore,
)
from agent_personal_memory.types import Actor  # noqa: E402
from agent_personal_memory.validation import content_hash_for  # noqa: E402
from score import aggregate, load_corpus, score_case  # noqa: E402

CLAIM_QUERY_KEYS = (
    "states",
    "claim_kinds",
    "valid_from",
    "valid_to",
    "include_content",
    "limit",
)
SOURCE_QUERY_KEYS = (
    "source_kinds",
    "observed_from",
    "observed_to",
    "include_payload",
    "limit",
)


def _git_commit(root: Path = REPO_ROOT) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() or "unknown"


def prepare_out_dir(path: Path, *, force_clean: bool = False) -> Path:
    """Create an empty output directory, refusing stale artifacts by default."""
    path = Path(path).resolve()
    if path.exists() and not path.is_dir():
        raise ValueError(f"output path is not a directory: {path}")
    if path.is_dir():
        children = list(path.iterdir())
        if children and not force_clean:
            raise ValueError(
                f"output directory is not empty: {path}; use --force-clean-out"
            )
        if force_clean:
            for child in children:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _as_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise TypeError("snapshot/content must be a string or bytes")


def _public_claim_row(row: dict[str, object]) -> dict[str, object]:
    return {
        "claim_id": str(row["claim_id"]),
        "claim_kind": str(row["claim_kind"]),
        "state": str(row["state"]),
        "sensitivity": str(row["sensitivity"]),
        "confidence": row["confidence"],
        "expires_at": row.get("expires_at"),
    }


def _public_source_row(row: dict[str, object]) -> dict[str, object]:
    return {
        "source_id": str(row["source_id"]),
        "source_kind": str(row["source_kind"]),
        "deletion_state": str(row["deletion_state"]),
        "observed_at": str(row["observed_at"]),
    }


def _select_kwargs(args: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: args[key] for key in keys if key in args}


def _require_temp_store_dir(data_dir: Path) -> Path:
    resolved = Path(data_dir).resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if not resolved.is_relative_to(temp_root):
        raise ValueError(f"bench store must use a temp directory, not {resolved}")
    return resolved


def _actors(case: dict[str, Any]) -> dict[str, Actor]:
    actors = {}
    for alias, spec in (case.get("actors") or {}).items():
        actors[str(alias)] = Actor(
            platform=str(spec["platform"]),
            user_id=str(spec["user_id"]),
            display_name=str(spec.get("display_name") or ""),
        )
    return actors


def seed_case(
    case: dict[str, Any],
    store: PersonalMemoryStore,
) -> tuple[dict[str, Actor], dict[str, str]]:
    """Create the case fixture in an isolated store and return alias-to-id maps."""
    actors = _actors(case)
    alias_ids: dict[str, str] = {}
    seed = case.get("seed") or {}

    for source in seed.get("sources") or []:
        snapshot = _as_bytes(source["snapshot"])
        row = store.register_source(
            actors[str(source["actor"])],
            source_kind=str(source["source_kind"]),
            external_ref=str(source["external_ref"]),
            content_hash=content_hash_for(snapshot),
            snapshot=snapshot,
            observed_at=str(source["observed_at"]),
            retention_class=str(source["retention_class"]),
            metadata=dict(source.get("metadata") or {}),
        )
        alias_ids[str(source["alias"])] = str(row["source_id"])

    for claim in seed.get("claims") or []:
        content = _as_bytes(claim["content"])
        source_ids = [alias_ids[str(alias)] for alias in claim["source_aliases"]]
        row = store.create_candidate(
            actors[str(claim["actor"])],
            claim_kind=str(claim["claim_kind"]),
            content=content,
            confidence=float(claim["confidence"]),
            explanation=claim.get("explanation"),
            sensitivity=str(claim["sensitivity"]),
            source_ids=source_ids,
            valid_from=claim.get("valid_from"),
            valid_to=claim.get("valid_to"),
            expires_at=str(claim["expires_at"]),
        )
        claim_id = str(row["claim_id"])
        alias_ids[str(claim["alias"])] = claim_id
        if claim.get("confirm"):
            store.confirm_claim(
                actors[str(claim["actor"])],
                claim_id,
                confirmation_source_id=alias_ids[str(claim["confirm_source_alias"])],
            )
        if claim.get("erase"):
            store.erase_claim(actors[str(claim["actor"])], claim_id)

    for mutation in seed.get("mutations") or []:
        op = str(mutation["op"])
        alias = str(mutation["alias"])
        if op == "erase_source":
            store.erase_source(actors[str(mutation["actor"])], alias_ids[alias])
        elif op == "expire_claim":
            conn = store._connect()
            try:
                conn.execute(
                    "UPDATE memory_claim_versions SET expires_at = ? WHERE claim_id = ?",
                    (str(mutation["expires_at"]), alias_ids[alias]),
                )
                conn.commit()
            finally:
                conn.close()
        else:
            raise ValueError(f"unsupported seed mutation: {op}")
    return actors, alias_ids


def _error_payload(exc: Exception) -> dict[str, str]:
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "repr": repr(exc),
    }


def run_query(
    query: dict[str, Any],
    actors: dict[str, Actor],
    alias_ids: dict[str, str],
    store: PersonalMemoryStore,
) -> dict[str, Any]:
    op = str(query["op"])
    actor = actors[str(query["actor"])]
    args = dict(query.get("args") or {})
    result: dict[str, Any] = {
        "id": query["id"],
        "op": op,
        "claim_ids": [],
        "source_ids": [],
        "rows": [],
        "error": None,
    }
    try:
        if op == "query_claims":
            rows = query_claims(actor, store=store, **_select_kwargs(args, CLAIM_QUERY_KEYS))
            result["rows"] = [_public_claim_row(row) for row in rows]
            result["claim_ids"] = [str(row["claim_id"]) for row in rows]
        elif op == "query_sources":
            rows = query_sources(actor, store=store, **_select_kwargs(args, SOURCE_QUERY_KEYS))
            result["rows"] = [_public_source_row(row) for row in rows]
            result["source_ids"] = [str(row["source_id"]) for row in rows]
        elif op == "get_authorized_claim":
            claim_id = str(args.get("claim_id") or alias_ids[str(args["claim_alias"])])
            row = get_authorized_claim(actor, claim_id, store=store)
            result["rows"] = [_public_claim_row(row)]
            result["claim_ids"] = [str(row["claim_id"])]
        elif op == "get_authorized_source":
            source_id = str(args.get("source_id") or alias_ids[str(args["source_alias"])])
            row = get_authorized_source(actor, source_id, store=store)
            result["rows"] = [_public_source_row(row)]
            result["source_ids"] = [str(row["source_id"])]
        else:
            raise ValueError(f"unsupported query op: {op}")
    except NotFoundError as exc:
        result["error"] = _error_payload(exc)
    return result


def run_component_case(
    case: dict[str, Any],
    repeat: int = 1,
    *,
    corpus_path: Path = HERE / "corpus.json",
) -> dict[str, Any]:
    """Run one corpus case against a freshly seeded isolated store."""
    with tempfile.TemporaryDirectory(prefix="hermes-pm-bench-") as td:
        data_dir = _require_temp_store_dir(Path(td))
        store = PersonalMemoryStore(data_dir=data_dir)
        actors, alias_ids = seed_case(case, store)
        operations = [
            run_query(query, actors, alias_ids, store) for query in case["queries"]
        ]
        return {
            "case_id": case["id"],
            "variant": "component",
            "repeat": repeat,
            "store_dir": str(data_dir),
            "alias_ids": alias_ids,
            "operations": operations,
            "prompt": dict(case.get("prompt") or {}),
            "metadata": {
                "corpus_path": str(Path(corpus_path).resolve()),
                "artifact_filename": f"component.{case['id']}.r{repeat}.json",
            },
        }


def write_report(
    cases: list[dict[str, Any]],
    trajectories: list[dict[str, Any]],
    out: Path,
    *,
    metadata: dict[str, Any] | None = None,
) -> int:
    cases_by_id = {case["id"]: case for case in cases}
    results = []
    for trajectory in trajectories:
        case = cases_by_id.get(trajectory.get("case_id"))
        if not case:
            raise ValueError(f"Unknown case_id {trajectory.get('case_id')}")
        results.append(score_case(case, trajectory))
    report_metadata: dict[str, Any] = {
        "harness_commit": _git_commit(),
        "case_count": len(cases),
        "trajectory_count": len(trajectories),
    }
    if metadata:
        report_metadata.update(metadata)
    report = {
        "metadata": report_metadata,
        "summary": aggregate(results),
        "results": results,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"wrote {out}")
    return 0 if all(result["passed"] for result in results) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=HERE / "corpus.json")
    sub = parser.add_subparsers(dest="command", required=True)
    component = sub.add_parser("component")
    component.add_argument("--repeats", type=int, default=1)
    component.add_argument(
        "--out-dir",
        type=Path,
        default=HERE / "results" / "component",
    )
    component.add_argument("--force-clean-out", action="store_true")
    args = parser.parse_args(argv)
    corpus = load_corpus(args.corpus)
    cases = corpus["cases"]
    if args.command == "component":
        try:
            out_dir = prepare_out_dir(
                args.out_dir,
                force_clean=args.force_clean_out,
            )
        except ValueError as exc:
            parser.error(str(exc))
        trajectories = [
            run_component_case(
                case,
                repeat,
                corpus_path=args.corpus,
            )
            for repeat in range(1, args.repeats + 1)
            for case in cases
        ]
        for trajectory in trajectories:
            name = trajectory["metadata"]["artifact_filename"]
            (out_dir / name).write_text(
                json.dumps(trajectory, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return write_report(
            cases,
            trajectories,
            out_dir / "report.json",
            metadata={"corpus_version": corpus["corpus_version"]},
        )
    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

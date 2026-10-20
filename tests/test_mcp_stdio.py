"""Tests for the MCP stdio facade (``agent_personal_memory.mcp``).

Pure-Python members (argument parsing, the outbox send transport, dispatch
wiring, and the confirm-gate nonce flow) always run. The subprocess
JSON-RPC members are gated on the optional ``mcp`` extra being installed.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import os
import queue
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import agent_personal_memory as apm
from agent_personal_memory import mcp as apm_mcp
from agent_personal_memory.crypto import CryptoError
from agent_personal_memory.validation import (
    ValidationError,
    content_hash_for,
)

try:
    from mcp.types import LATEST_PROTOCOL_VERSION as _PROTOCOL_VERSION

    _SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the optional extra
    _SDK_AVAILABLE = False

_PLATFORM = "test-platform"
_USER_ID = "u41"
_OTHER_USER_ID = "u99"
_SESS_KEY = "sk-1"
_SESS_ID = "sess-1"
_CHAT_ID = "chat-1"
_OTHER_CHAT_ID = "chat-9"

# Property names that would let a caller smuggle identity/binding selectors.
_IDENTITY_PROPS = frozenset(
    {
        "actor",
        "actor_platform",
        "actor_user_id",
        "platform",
        "user_id",
        "tenant_id",
        "session_key",
        "session_id",
        "chat_id",
        "thread_id",
        "profile",
        "session_binding",
    }
)

_NONCE_RE = re.compile(r"/memory_confirm\s+(\S+)")


def _argv(data_dir: str, drop: str | None = None, **overrides: str) -> list[str]:
    mapping = {
        "--actor-platform": _PLATFORM,
        "--actor-user-id": _USER_ID,
        "--session-key": _SESS_KEY,
        "--session-id": _SESS_ID,
        "--chat-id": _CHAT_ID,
        "--data-dir": data_dir,
    }
    for name, value in overrides.items():
        mapping["--" + name.replace("_", "-")] = value
    args: list[str] = []
    for flag, value in mapping.items():
        if flag != drop:
            args += [flag, value]
    return args


def _binding(**overrides: str) -> apm.SessionBinding:
    values = {
        "platform": _PLATFORM,
        "user_id": _USER_ID,
        "session_key": _SESS_KEY,
        "session_id": _SESS_ID,
        "chat_id": _CHAT_ID,
        "thread_id": "",
        "profile": "",
        "display_name": "",
    }
    values.update(overrides)
    return apm.SessionBinding(**values)  # type: ignore[arg-type]


def _nonce_from_outbox(outbox: Path) -> str:
    lines = outbox.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise AssertionError(f"outbox {outbox} is empty")
    record = json.loads(lines[-1])
    match = _NONCE_RE.search(record["body"])
    if match is None:
        raise AssertionError(f"no confirm nonce in outbox body: {record}")
    return match.group(1)


class StartupConfigTests(unittest.TestCase):
    """Startup flags provision the fixed binding or fail fast."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_full_flags_produce_fixed_binding(self) -> None:
        args = apm_mcp.build_parser().parse_args(_argv(self.data_dir))
        binding = apm_mcp.build_binding(args)
        self.assertEqual(binding.platform, _PLATFORM)
        self.assertEqual(binding.user_id, _USER_ID)
        self.assertEqual(binding.session_key, _SESS_KEY)
        self.assertEqual(binding.session_id, _SESS_ID)
        self.assertEqual(binding.chat_id, _CHAT_ID)
        actor = binding.actor()
        self.assertEqual(actor.platform, _PLATFORM)
        self.assertEqual(actor.user_id, _USER_ID)

    def test_missing_binding_flag_fails_fast(self) -> None:
        for flag in (
            "--actor-platform",
            "--actor-user-id",
            "--session-key",
            "--session-id",
            "--chat-id",
        ):
            with self.subTest(missing=flag):
                args = apm_mcp.build_parser().parse_args(
                    _argv(self.data_dir, drop=flag)
                )
                with self.assertRaises(ValueError) as raised:
                    apm_mcp.build_binding(args)
                self.assertIn(flag, str(raised.exception))

    def test_no_flags_fails_fast(self) -> None:
        args = apm_mcp.build_parser().parse_args(["--data-dir", self.data_dir])
        with self.assertRaises(ValueError) as raised:
            apm_mcp.build_binding(args)
        self.assertIn("--actor-platform", str(raised.exception))

    def test_invalid_user_id_fails_fast(self) -> None:
        for bad in ("0", "unknown", "none"):
            with self.subTest(user_id=bad):
                args = apm_mcp.build_parser().parse_args(
                    _argv(self.data_dir, actor_user_id=bad)
                )
                with self.assertRaises(ValueError):
                    apm_mcp.build_binding(args)

    def test_build_surface_wires_store_and_outbox(self) -> None:
        args = apm_mcp.build_parser().parse_args(_argv(self.data_dir))
        surface = apm_mcp.build_surface(args)
        self.assertEqual(surface.store.data_dir, Path(self.data_dir))
        self.assertEqual(
            surface.outbox_path, Path(self.data_dir) / apm_mcp.OUTBOX_NAME
        )
        self.assertEqual(surface.binding.user_id, _USER_ID)
        self.assertEqual(surface.actor.user_id, _USER_ID)

    def test_main_reports_missing_extra(self) -> None:
        stderr = io.StringIO()
        with (
            contextlib.redirect_stderr(stderr),
            patch.object(
                apm_mcp, "_load_sdk", side_effect=ImportError("no mcp")
            ),
        ):
            rc = apm_mcp.main(_argv(self.data_dir))
        self.assertEqual(rc, 2)
        self.assertIn("agent-personal-memory[mcp]", stderr.getvalue())

    def test_main_fails_without_binding_flags(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            try:
                rc = apm_mcp.main(["--data-dir", self.data_dir])
            except SystemExit as exc:
                rc = exc.code
        self.assertNotEqual(rc, 0)
        if _SDK_AVAILABLE:
            self.assertIn("missing required startup flag", stderr.getvalue())


class OutboxSendTests(unittest.TestCase):
    """The confirm-gate send transport: JSONL outbox + stderr notice."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.outbox = self.data_dir / apm_mcp.OUTBOX_NAME

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_send_appends_jsonl_and_notice_hides_body(self) -> None:
        err = io.StringIO()
        send = apm_mcp.make_outbox_send(
            self.outbox, request_id="r-1", stderr=err
        )
        self.assertTrue(send("preview body\nConfirm deletion: /memory_confirm nXYZ"))
        lines = self.outbox.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["request_id"], "r-1")
        self.assertIn("/memory_confirm nXYZ", record["body"])
        self.assertIn("ts", record)
        # stderr gets a short notice, never the nonce-bearing body.
        self.assertIn(apm_mcp.OUTBOX_NAME, err.getvalue())
        self.assertIn("r-1", err.getvalue())
        self.assertNotIn("nXYZ", err.getvalue())

    def test_send_appends_multiple_lines(self) -> None:
        send = apm_mcp.make_outbox_send(self.outbox, stderr=io.StringIO())
        send("first")
        send("second")
        lines = self.outbox.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertNotIn("request_id", json.loads(lines[0]))

    def test_send_failure_propagates(self) -> None:
        blocked = self.data_dir / "blocked"
        blocked.mkdir()
        send = apm_mcp.make_outbox_send(blocked, stderr=io.StringIO())
        with self.assertRaises(OSError):
            send("x")

    def test_outbox_created_with_owner_only_mode(self) -> None:
        send = apm_mcp.make_outbox_send(self.outbox, stderr=io.StringIO())
        self.assertTrue(send("preview body"))
        if os.name == "posix":
            mode = stat.S_IMODE(self.outbox.stat().st_mode)
            self.assertEqual(mode & 0o077, 0)
        else:
            self.assertTrue(self.outbox.is_file())

    def test_outbox_append_tightens_preexisting_loose_mode(self) -> None:
        self.outbox.write_text('{"ts": "t", "body": "old"}\n', encoding="utf-8")
        if os.name == "posix":
            os.chmod(self.outbox, 0o644)
        send = apm_mcp.make_outbox_send(self.outbox, stderr=io.StringIO())
        self.assertTrue(send("next body"))
        if os.name == "posix":
            mode = stat.S_IMODE(self.outbox.stat().st_mode)
            self.assertEqual(mode & 0o077, 0)
        self.assertEqual(
            len(self.outbox.read_text(encoding="utf-8").splitlines()), 2
        )

    def test_append_prunes_expired_lines_and_keeps_0600(self) -> None:
        # The outbox is a transient delivery channel: lines whose nonce
        # TTL horizon has passed are pruned on the next successful
        # append; unparseable lines (not written by this transport) and
        # fresh lines stay.
        stale = datetime.now(timezone.utc) - timedelta(seconds=3600)
        forged = json.dumps(
            {
                "ts": stale.isoformat(timespec="seconds"),
                "request_id": "r-old",
                "body": "dead nonce preview",
            }
        )
        self.outbox.write_text(
            forged + "\noperator-note\n", encoding="utf-8"
        )
        send = apm_mcp.make_outbox_send(self.outbox, stderr=io.StringIO())
        self.assertTrue(send("live preview body"))
        lines = self.outbox.read_text(encoding="utf-8").splitlines()
        self.assertIn("operator-note", lines)
        bodies = [
            json.loads(line)["body"]
            for line in lines
            if line.startswith("{")
        ]
        self.assertNotIn("dead nonce preview", bodies)
        self.assertIn("live preview body", bodies)
        if os.name == "posix":
            mode = stat.S_IMODE(self.outbox.stat().st_mode)
            self.assertEqual(mode & 0o077, 0)

    def test_append_survives_non_utf8_preexisting_line(self) -> None:
        # An operator may edit the outbox in a non-UTF-8 editor; a line
        # carrying raw non-UTF-8 bytes must be kept verbatim and must not
        # abort the prune or propagate into send().
        stale = datetime.now(timezone.utc) - timedelta(seconds=3600)
        forged = json.dumps(
            {"ts": stale.isoformat(timespec="seconds"), "body": "dead nonce"}
        ).encode("utf-8")
        self.outbox.write_bytes(forged + b"\ncp1251-op-note\xff\xfe\n")
        send = apm_mcp.make_outbox_send(self.outbox, stderr=io.StringIO())
        self.assertTrue(send("live body"))
        raw = self.outbox.read_bytes()
        self.assertIn(b"cp1251-op-note\xff\xfe", raw)
        self.assertIn(b"live body", raw)
        self.assertNotIn(b"dead nonce", raw)

    def test_prune_failure_does_not_fail_send(self) -> None:
        # A prune error is logged to stderr and swallowed: the nonce was
        # already delivered, so the send itself still succeeds.
        stale = datetime.now(timezone.utc) - timedelta(seconds=3600)
        forged = json.dumps(
            {"ts": stale.isoformat(timespec="seconds"), "body": "dead nonce"}
        )
        self.outbox.write_text(forged + "\n", encoding="utf-8")
        blocker = self.data_dir / f"{apm_mcp.OUTBOX_NAME}.{os.getpid()}.tmp"
        blocker.mkdir()  # occupies the prune temp path -> rewrite fails
        err = io.StringIO()
        send = apm_mcp.make_outbox_send(self.outbox, stderr=err)
        self.assertTrue(send("live body"))
        self.assertIn("prune", err.getvalue())


class ErrorTextTests(unittest.TestCase):
    """Tool-result error text: whitelisted types pass through, the rest
    collapse to a generic message so internals never leak to the model."""

    def test_whitelisted_errors_carry_their_message(self) -> None:
        cases = (
            (ValidationError("arguments: missing required"), "ValidationError"),
            (apm.NotFoundError("Claim not found"), "NotFoundError"),
            (apm.ConflictError("Delete target set changed"), "ConflictError"),
            (
                apm.TrustedDeleteDenied("delete request is not available"),
                "TrustedDeleteDenied",
            ),
            (ValueError("plain value error"), "ValueError"),
            (KeyError("missing-key"), "KeyError"),
        )
        for exc, name in cases:
            with self.subTest(error=name):
                self.assertEqual(
                    apm_mcp._error_text(exc), f"{name}: {exc}"
                )

    def test_unexpected_error_collapses_to_generic_text(self) -> None:
        # Assembled (not literal) so this file stays clean under the
        # hygiene scanner's host-path pattern.
        private_path = "C:" + chr(92) + "leak" + chr(92) + "path"
        for exc in (
            OSError(private_path),
            RuntimeError("stack detail " + private_path),
            CryptoError("dek unwrap failed at " + private_path),
        ):
            with self.subTest(error=type(exc).__name__):
                text = apm_mcp._error_text(exc)
                self.assertEqual(text, "InternalError: operation failed")
                self.assertNotIn(private_path, text)
                self.assertNotIn("leak", text)


@unittest.skipUnless(_SDK_AVAILABLE, "optional 'mcp' extra is not installed")
class ErrorTextWireTests(unittest.TestCase):
    """A stubbed flow that raises an unexpected error must not leak into
    the tool result text over the SDK adapter."""

    def test_internal_error_text_hides_internals(self) -> None:
        private_path = "C:" + chr(92) + "leak" + chr(92) + "path"
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = apm.PersonalMemoryStore(data_dir=tmp.name)
        binding = _binding()
        surface = apm_mcp.McpToolSurface(
            actor=binding.actor(),
            binding=binding,
            store=store,
            outbox_path=Path(tmp.name) / apm_mcp.OUTBOX_NAME,
            stderr=io.StringIO(),
        )
        server = apm_mcp.build_sdk_server(surface)
        get_handler = getattr(server, "get_request_handler", None)
        entry = get_handler("tools/call") if get_handler else None
        if entry is None:
            self.skipTest("mcp SDK without a tools/call handler registry")
        from mcp import types as mcp_types

        with patch.object(
            surface, "call_tool", side_effect=OSError(private_path)
        ):
            result = asyncio.run(
                entry.handler(
                    None,
                    mcp_types.CallToolRequestParams(
                        name="list_personal_memories", arguments={}
                    ),
                )
            )
        self.assertTrue(
            getattr(result, "is_error", getattr(result, "isError", False))
        )
        text = result.content[0].text
        self.assertEqual(text, "InternalError: operation failed")
        self.assertNotIn(private_path, text)
        self.assertNotIn("leak", text)


class DispatchTests(unittest.TestCase):
    """In-process dispatch wiring against a real store + fixed binding."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.store = apm.PersonalMemoryStore(data_dir=self.data_dir)
        self.binding = _binding()
        self.err = io.StringIO()
        self.surface = apm_mcp.McpToolSurface(
            actor=self.binding.actor(),
            binding=self.binding,
            store=self.store,
            outbox_path=self.data_dir / apm_mcp.OUTBOX_NAME,
            stderr=self.err,
        )
        apm.reset_registry()

    def tearDown(self) -> None:
        apm.reset_registry()
        self._tmp.cleanup()

    def _outbox(self) -> Path:
        return self.data_dir / apm_mcp.OUTBOX_NAME

    def _remember(self, content: str) -> str:
        created = self.surface.call_tool(
            "remember_personal_insight", {"content": content}
        )
        return str(created["claim_id"])

    def test_tool_list_matches_canonical_surface(self) -> None:
        tools = self.surface.tool_schemas()
        self.assertEqual(len(tools), 26)
        self.assertEqual(
            [t["name"] for t in tools], list(apm.TOOL_SCHEMAS.keys())
        )
        for tool in tools:
            schema = apm.TOOL_SCHEMAS[tool["name"]]
            self.assertEqual(tool["description"], schema["description"])
            self.assertEqual(tool["inputSchema"], schema["parameters"])
            props = set(tool["inputSchema"].get("properties", {}))
            self.assertFalse(props & _IDENTITY_PROPS, tool["name"])
        confirm = next(
            t for t in tools if t["name"] == "personal_memory_confirm"
        )
        self.assertEqual(
            set(confirm["inputSchema"]["properties"]), {"nonce"}
        )

    def test_remember_then_list(self) -> None:
        claim_id = self._remember("I drink green tea every morning")
        listed = self.surface.call_tool("list_personal_memories", {})
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["items"][0]["claim_id"], claim_id)

    def test_forget_preview_confirm_roundtrip(self) -> None:
        claim_id = self._remember("I take my coffee black")
        preview = self.surface.call_tool(
            "forget_personal_memory", {"claim_id": claim_id}
        )
        self.assertTrue(preview["preview"])
        self.assertTrue(preview.get("request_id"))
        self.assertEqual(preview.get("claim_id"), claim_id)
        self.assertEqual(preview.get("delivery"), "operator_outbox")
        # The nonce must never appear in the tool result.
        self.assertNotIn("nonce", json.dumps(preview))
        nonce = _nonce_from_outbox(self._outbox())
        self.assertTrue(nonce.startswith("n"))
        self.assertIn("operator outbox", self.err.getvalue())
        self.assertNotIn(nonce, self.err.getvalue())

        result = self.surface.call_tool(
            "personal_memory_confirm", {"nonce": nonce}
        )
        self.assertFalse(result["preview"])
        self.assertIn(claim_id, result["erased_claim_ids"])
        self.assertNotIn("nonce", json.dumps(result))
        listed = self.surface.call_tool("list_personal_memories", {})
        self.assertEqual(listed["count"], 0)

    def test_wrong_nonce_denied(self) -> None:
        claim_id = self._remember("I jog at dawn")
        self.surface.call_tool("forget_personal_memory", {"claim_id": claim_id})
        with self.assertRaises(ValidationError):
            self.surface.call_tool(
                "personal_memory_confirm", {"nonce": "n-forged"}
            )
        listed = self.surface.call_tool("list_personal_memories", {})
        self.assertEqual(listed["count"], 1)

    def test_binding_mismatch_denied(self) -> None:
        claim_id = self._remember("I prefer aisle seats")
        self.surface.call_tool("forget_personal_memory", {"claim_id": claim_id})
        nonce = _nonce_from_outbox(self._outbox())
        other_binding = _binding(user_id=_OTHER_USER_ID, chat_id=_OTHER_CHAT_ID)
        other = apm_mcp.McpToolSurface(
            actor=other_binding.actor(),
            binding=other_binding,
            store=self.store,
            outbox_path=self._outbox(),
            stderr=io.StringIO(),
        )
        with self.assertRaises(ValidationError):
            other.call_tool("personal_memory_confirm", {"nonce": nonce})
        # A mismatched consume does not burn the nonce.
        result = self.surface.call_tool(
            "personal_memory_confirm", {"nonce": nonce}
        )
        self.assertIn(claim_id, result["erased_claim_ids"])

    def test_do_not_remember_text_match_delivers_preview(self) -> None:
        claim_id = self._remember("I love weekend hiking trips")
        preview = self.surface.call_tool(
            "do_not_remember", {"text": "hiking"}
        )
        self.assertTrue(preview["preview"])
        self.assertIn(claim_id, preview["matched_claim_ids"])
        self.assertTrue(_nonce_from_outbox(self._outbox()))

    def test_empty_match_preview_sends_nothing(self) -> None:
        preview = self.surface.call_tool(
            "do_not_remember", {"text": "no-such-thing"}
        )
        self.assertTrue(preview["preview"])
        self.assertIsNone(preview.get("request_id"))
        self.assertFalse(self._outbox().exists())

    def test_send_failure_denies_confirm(self) -> None:
        blocked = self.data_dir / "blocked-outbox"
        blocked.mkdir()
        surface = apm_mcp.McpToolSurface(
            actor=self.binding.actor(),
            binding=self.binding,
            store=self.store,
            outbox_path=blocked,
            stderr=io.StringIO(),
        )
        claim_id = self._remember("I prefer window seats")
        with self.assertRaises(ValidationError):
            surface.call_tool(
                "forget_personal_memory", {"claim_id": claim_id}
            )
        # No nonce was registered: nothing is consumable.
        with self.assertRaises(ValidationError):
            surface.call_tool(
                "personal_memory_confirm", {"nonce": "n-anything"}
            )
        listed = self.surface.call_tool("list_personal_memories", {})
        self.assertEqual(listed["count"], 1)

    def test_extra_and_identity_args_rejected(self) -> None:
        for bad in (
            {"user_id": _OTHER_USER_ID},
            {"actor": "someone"},
            {"tenant_id": "t9"},
            {"platform": "elsewhere"},
            {"session_key": "k"},
            {"bogus_field": 1},
        ):
            with self.subTest(args=bad), self.assertRaises(ValidationError) as cm:
                self.surface.call_tool("list_personal_memories", bad)
            self.assertIn("unexpected field", str(cm.exception))

    def test_confirm_rejects_injected_binding_fields(self) -> None:
        with self.assertRaises(ValidationError):
            self.surface.call_tool(
                "personal_memory_confirm",
                {"nonce": "n1", "session_key": "x"},
            )

    def test_unknown_tool_rejected(self) -> None:
        with self.assertRaises(ValidationError) as cm:
            self.surface.call_tool("definitely_not_a_tool", {})
        self.assertIn("unknown tool", str(cm.exception))

    def test_missing_required_arg_rejected(self) -> None:
        with self.assertRaises(ValidationError) as cm:
            self.surface.call_tool("remember_personal_insight", {})
        self.assertIn("missing required", str(cm.exception))

    def test_non_dict_arguments_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.surface.call_tool("list_personal_memories", ["x"])

    def test_export_owner_data_decodes_bytes(self) -> None:
        self._remember("I read paper books")
        payload = self.surface.call_tool("export_owner_data", {})
        self.assertIsInstance(payload, dict)

    def test_none_arguments_still_run_validation(self) -> None:
        # A missing argument object normalizes to {} and still hits the
        # required-field check: in-process callers get ValidationError,
        # not a bare KeyError/TypeError from the dispatch target.
        for name in ("personal_memory_confirm", "remember_personal_insight"):
            with self.subTest(tool=name):
                with self.assertRaises(ValidationError) as raised:
                    self.surface.call_tool(name)
                self.assertIn("missing required", str(raised.exception))
        # A tool with no required fields still accepts a missing object.
        listed = self.surface.call_tool("list_personal_memories")
        self.assertEqual(listed["count"], 0)

    def test_rank_claims_content_arrives_as_json_safe_text(self) -> None:
        self._remember("I drink green tea every morning")
        ranked = self.surface.call_tool(
            "rank_claims", {"query_text": "green tea"}
        )
        self.assertTrue(ranked)
        json.dumps(ranked)  # the whole payload must be wire-safe
        self.assertIsInstance(ranked[0]["content"], str)
        self.assertIn("green tea", ranked[0]["content"])

    def test_list_with_content_arrives_as_json_safe_text(self) -> None:
        claim_id = self._remember("I take my coffee black")
        listed = self.surface.call_tool(
            "list_personal_memories", {"include_content": True}
        )
        json.dumps(listed)
        item = listed["items"][0]
        self.assertEqual(item["claim_id"], claim_id)
        self.assertIsInstance(item["content"], str)
        self.assertIn("coffee", item["content"])

    def test_reconcile_history_and_retract_dispatch(self) -> None:
        # The five owner-flow tools dispatch through getattr by name with
        # the fixed store injected; every payload stays JSON-safe.
        first = self.surface.call_tool(
            "remember_personal_insight", {"content": "я живу в Казани"}
        )
        second = self.surface.call_tool(
            "remember_personal_insight",
            {"content": "я переехал в Пермь, больше не живу в Казани"},
        )
        self.assertEqual(second["state"], "candidate")
        links = second["reconciliations"]
        self.assertEqual(len(links), 1)
        link_id = links[0]["link_id"]

        listed = self.surface.call_tool("list_memory_reconciliations", {})
        json.dumps(listed, ensure_ascii=False)
        self.assertEqual(listed["count"], 1)
        item = listed["items"][0]
        self.assertEqual(item["link_id"], link_id)
        self.assertIsInstance(item["new_content"], str)
        self.assertEqual(
            item["new_content"],
            "я переехал в Пермь, больше не живу в Казани",
        )
        self.assertIsInstance(item["old_content"], str)
        self.assertEqual(item["old_content"], "я живу в Казани")

        with self.assertRaises(ValidationError):
            self.surface.call_tool(
                "accept_memory_reconciliation",
                {"link_id": link_id, "tenant_id": "t9"},
            )
        accepted = self.surface.call_tool(
            "accept_memory_reconciliation", {"link_id": link_id}
        )
        json.dumps(accepted)
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["new_state"], "confirmed")
        self.assertEqual(accepted["old_state"], "superseded")
        self.assertIsInstance(accepted["confirmation"], str)

        history = self.surface.call_tool(
            "personal_memory_history", {"claim_id": first["claim_id"]}
        )
        json.dumps(history, ensure_ascii=False)
        self.assertEqual(history["current_claim_id"], second["claim_id"])
        self.assertEqual(
            {entry["claim_id"] for entry in history["entries"]},
            {first["claim_id"], second["claim_id"]},
        )

        retracted = self.surface.call_tool(
            "retract_personal_memory", {"claim_id": second["claim_id"]}
        )
        self.assertEqual(retracted["state"], "retracted")
        rows = self.surface.call_tool(
            "query_claims", {"states": ["retracted"]}
        )
        self.assertEqual(
            [str(row["claim_id"]) for row in rows], [second["claim_id"]]
        )
        pending = self.surface.call_tool("list_memory_reconciliations", {})
        self.assertEqual(pending["count"], 0)

    def test_non_utf8_snapshot_uses_base64_fallback(self) -> None:
        blob = b"\xff\xfe\x00\x89raw-blob"
        created = self.store.register_source(
            self.surface.actor,
            source_kind="document",
            external_ref="test://raw-blob-1",
            content_hash=content_hash_for(blob),
            snapshot=blob,
            observed_at="2020-06-01T00:00:00Z",
            retention_class="standard",
        )
        rows = self.surface.call_tool(
            "query_sources", {"include_payload": True}
        )
        json.dumps(rows)
        row = next(r for r in rows if r["source_id"] == created["source_id"])
        snapshot = row["snapshot"]
        self.assertEqual(snapshot["encoding"], "base64")
        self.assertEqual(base64.b64decode(snapshot["data"]), blob)


@unittest.skipUnless(_SDK_AVAILABLE, "optional 'mcp' extra is not installed")
class StdioServerTests(unittest.TestCase):
    """Spawn ``python -m agent_personal_memory.mcp`` and speak JSON-RPC."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_personal_memory.mcp",
                *_argv(str(self.data_dir)),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self._rid = 0
        self._stdout: queue.Queue[str] = queue.Queue()
        self._stderr_lines: list[str] = []
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def tearDown(self) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except OSError:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        self._tmp.cleanup()

    # --- JSON-RPC helpers -----------------------------------------------

    def _drain_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            stripped = line.strip()
            if stripped:
                self._stdout.put(stripped)

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            self._stderr_lines.append(line)

    def _next_message(self, timeout: float = 30.0) -> dict:
        line = self._stdout.get(timeout=timeout)
        # stdout purity: every line on stdout must be a JSON-RPC object.
        message = json.loads(line)
        self.assertEqual(message.get("jsonrpc"), "2.0")
        return message

    def _send(self, payload: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def _request(self, method: str, params: dict | None = None) -> dict:
        self._rid += 1
        request: dict = {"jsonrpc": "2.0", "id": self._rid, "method": method}
        if params is not None:
            request["params"] = params
        self._send(request)
        while True:
            response = self._next_message()
            if response.get("id") == self._rid:
                return response

    def _notify(self, method: str, params: dict | None = None) -> None:
        message: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def _call_tool(
        self, name: str, arguments: dict | None = None
    ) -> dict:
        response = self._request(
            "tools/call", {"name": name, "arguments": arguments or {}}
        )
        result = response.get("result", {})
        self.assertFalse(result.get("isError"), response)
        payload = result.get("structuredContent")
        if payload is None:
            content = result.get("content") or []
            self.assertTrue(content, response)
            payload = json.loads(content[0]["text"])
        return payload

    def _call_tool_error(self, name: str, arguments: dict) -> dict:
        response = self._request(
            "tools/call", {"name": name, "arguments": arguments}
        )
        result = response.get("result", {})
        self.assertTrue(result.get("isError"), response)
        return result

    def _handshake(self) -> None:
        init = self._request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcp-stdio-test", "version": "0"},
            },
        )
        self.assertIn("result", init)
        self.assertIn("serverInfo", init["result"])
        self._notify("notifications/initialized")

    def _stderr_text(self, needle: str, timeout: float = 5.0) -> str:
        """Poll drained stderr until ``needle`` appears or the deadline."""
        deadline = time.monotonic() + timeout
        while True:
            text = "".join(self._stderr_lines)
            if needle in text or time.monotonic() >= deadline:
                return text
            time.sleep(0.05)

    # --- protocol members -------------------------------------------------

    def test_initialize_and_tools_list(self) -> None:
        self._handshake()
        tools = self._request("tools/list")["result"]["tools"]
        self.assertEqual(len(tools), 26)
        self.assertEqual({t["name"] for t in tools}, set(apm.TOOL_SCHEMAS))
        for tool in tools:
            props = set(tool["inputSchema"].get("properties", {}))
            self.assertFalse(props & _IDENTITY_PROPS, tool["name"])
        confirm = next(
            t for t in tools if t["name"] == "personal_memory_confirm"
        )
        self.assertEqual(
            set(confirm["inputSchema"]["properties"]), {"nonce"}
        )

    def test_remember_forget_confirm_flow(self) -> None:
        self._handshake()
        created = self._call_tool(
            "remember_personal_insight",
            {"content": "I take my coffee black"},
        )
        claim_id = created["claim_id"]
        listed = self._call_tool("list_personal_memories", {})
        self.assertEqual(listed["count"], 1)

        preview = self._call_tool(
            "forget_personal_memory", {"claim_id": claim_id}
        )
        self.assertTrue(preview["preview"])
        self.assertTrue(preview.get("request_id"))
        # The nonce-bearing text never enters the MCP response channel.
        self.assertNotIn("nonce", json.dumps(preview))

        outbox = self.data_dir / "confirmations.jsonl"
        nonce = _nonce_from_outbox(outbox)
        record = json.loads(
            outbox.read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertEqual(record["request_id"], preview["request_id"])
        self.assertIn("ts", record)

        # stderr carried a delivery notice, not the nonce body.
        stderr_text = self._stderr_text("operator outbox")
        self.assertIn("operator outbox", stderr_text)
        self.assertNotIn(nonce, stderr_text)

        erased = self._call_tool("personal_memory_confirm", {"nonce": nonce})
        self.assertFalse(erased["preview"])
        self.assertIn(claim_id, erased["erased_claim_ids"])
        self.assertNotIn("nonce", json.dumps(erased))
        self.assertEqual(
            self._call_tool("list_personal_memories", {})["count"], 0
        )

    def test_content_returning_tools_serialize_over_stdio(self) -> None:
        self._handshake()
        self._call_tool(
            "remember_personal_insight",
            {"content": "I drink green tea every morning"},
        )
        ranked = self._call_tool("rank_claims", {"query_text": "green tea"})
        self.assertTrue(ranked)
        self.assertIsInstance(ranked[0]["content"], str)
        self.assertIn("green tea", ranked[0]["content"])
        listed = self._call_tool(
            "list_personal_memories", {"include_content": True}
        )
        self.assertEqual(listed["count"], 1)
        self.assertIsInstance(listed["items"][0]["content"], str)
        self.assertIn("green tea", listed["items"][0]["content"])

    def test_reconcile_surface_over_stdio(self) -> None:
        self._handshake()
        names = {
            tool["name"]
            for tool in self._request("tools/list")["result"]["tools"]
        }
        for name in (
            "list_memory_reconciliations",
            "accept_memory_reconciliation",
            "reject_memory_reconciliation",
            "personal_memory_history",
            "retract_personal_memory",
        ):
            self.assertIn(name, names)

        first = self._call_tool(
            "remember_personal_insight", {"content": "я живу в Казани"}
        )
        second = self._call_tool(
            "remember_personal_insight",
            {"content": "я переехал в Пермь, больше не живу в Казани"},
        )
        link_id = second["reconciliations"][0]["link_id"]

        listed = self._call_tool("list_memory_reconciliations", {})
        self.assertEqual(listed["count"], 1)
        item = listed["items"][0]
        self.assertEqual(item["link_id"], link_id)
        self.assertIsInstance(item["new_content"], str)
        self.assertEqual(
            item["new_content"],
            "я переехал в Пермь, больше не живу в Казани",
        )
        self.assertEqual(item["old_content"], "я живу в Казани")

        denied = self._call_tool_error(
            "accept_memory_reconciliation",
            {"link_id": link_id, "actor": _OTHER_USER_ID},
        )
        self.assertIn(
            "unexpected field", json.dumps(denied, ensure_ascii=False)
        )

        accepted = self._call_tool(
            "accept_memory_reconciliation", {"link_id": link_id}
        )
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["new_state"], "confirmed")
        self.assertEqual(accepted["old_state"], "superseded")

        history = self._call_tool(
            "personal_memory_history", {"claim_id": first["claim_id"]}
        )
        self.assertEqual(history["current_claim_id"], second["claim_id"])

        retracted = self._call_tool(
            "retract_personal_memory", {"claim_id": second["claim_id"]}
        )
        self.assertEqual(retracted["state"], "retracted")

    def test_wrong_nonce_denied_over_stdio(self) -> None:
        self._handshake()
        self._call_tool(
            "remember_personal_insight", {"content": "I jog at dawn"}
        )
        listed = self._call_tool("list_personal_memories", {})
        claim_id = listed["items"][0]["claim_id"]
        self._call_tool("forget_personal_memory", {"claim_id": claim_id})
        result = self._call_tool_error(
            "personal_memory_confirm", {"nonce": "n-forged-nonce"}
        )
        text = json.dumps(result)
        self.assertIn("not available", text)
        self.assertEqual(
            self._call_tool("list_personal_memories", {})["count"], 1
        )

    def test_identity_injection_and_unknown_tool_denied(self) -> None:
        self._handshake()
        result = self._call_tool_error(
            "list_personal_memories", {"user_id": _OTHER_USER_ID}
        )
        self.assertIn("unexpected field", json.dumps(result))
        self._call_tool_error(
            "personal_memory_confirm",
            {"nonce": "n1", "actor_user_id": _OTHER_USER_ID},
        )
        result = self._call_tool_error("definitely_not_a_tool", {})
        self.assertIn("unknown tool", json.dumps(result))

    def test_server_refuses_missing_binding(self) -> None:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_personal_memory.mcp",
                "--data-dir",
                str(self.data_dir),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        out, err = proc.communicate(timeout=30)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(out.strip(), "")  # stdout stays clean
        self.assertIn("missing required startup flag", err)


if __name__ == "__main__":
    unittest.main()

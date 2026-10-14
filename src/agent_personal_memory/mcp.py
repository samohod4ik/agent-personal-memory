"""Thin MCP stdio facade over the owner-flow API.

Runs an MCP server over stdio (newline-delimited JSON-RPC) using the
optional ``mcp`` SDK extra. The operator identity and session binding are
fixed at startup via flags; tool arguments can never carry actor, tenant,
or binding selectors.

Confirm-gate transport: a delivered preview is appended to the
operator-only outbox ``<data_dir>/confirmations.jsonl`` (one JSON line per
send: ``{"ts", "request_id", "body"}``) plus a short stderr notice. stdout
carries JSON-RPC only. The one-time nonce exists solely inside the outbox
payload - never in a tool result - so the model cannot authorize its own
deletion request; the operator reads the nonce from the outbox and calls
``personal_memory_confirm`` with it. The outbox is a transient delivery
channel, not an audit log: lines whose nonce TTL horizon has passed are
pruned on the next successful append.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import agent_personal_memory as apm
from .confirmation import _REQUEST_TTL_SECONDS, _binding_complete
from .store import PersonalMemoryStore
from .validation import ValidationError

SERVER_NAME = "agent-personal-memory"
OUTBOX_NAME = "confirmations.jsonl"

# Outbox retention horizon: the nonce TTL plus a small clock-skew margin.
# Lines whose ``ts`` predates it hold only dead nonces, so they are pruned
# on the next successful append instead of accumulating forever.
_OUTBOX_RETAIN_SECONDS = _REQUEST_TTL_SECONDS + 100.0
INSTALL_HINT = (
    "the MCP server requires the optional 'mcp' extra - "
    'install with: pip install "agent-personal-memory[mcp]"'
)

# Destructive tools produce a non-authorizing preview; the facade then opens
# it through the operator outbox so the nonce is delivered off-channel.
_PREVIEW_TOOLS = frozenset(
    {"forget_personal_memory", "do_not_remember", "reject_candidate_memory"}
)

# Owner-flow functions whose store kwarg is not named ``store``.
_STORE_KWARG = {"assemble_context": "memory_store"}

# Startup flags mapped to SessionBinding fields, in check order.
_BINDING_FLAGS = (
    ("--actor-platform", "platform"),
    ("--actor-user-id", "user_id"),
    ("--session-key", "session_key"),
    ("--session-id", "session_id"),
    ("--chat-id", "chat_id"),
)

# Defensive: a nonce-shaped key must never reach a tool result.
_NONCE_KEYS = frozenset({"nonce", "confirm_nonce", "confirmation_nonce"})

# Exception names whose message may appear in a tool result: known
# library/argument failures. Anything else collapses to a generic text so
# host paths and unexpected internals never reach the model channel (the
# MRO walk also covers subclasses like TrustedDeleteDenied).
_SAFE_ERROR_NAMES = frozenset(
    {
        "ValidationError",
        "NotFoundError",
        "ConflictError",
        "ForbiddenError",
        "TrustedDeleteDenied",
        "ValueError",
        "KeyError",
    }
)


def _error_text(exc: Exception) -> str:
    """Tool-result text for a failed call.

    Whitelisted exception types carry ``TypeName: message``; everything
    else becomes a generic internal-error line — an unexpected failure can
    carry host paths or other internals in ``str(exc)``.
    """
    if any(cls.__name__ in _SAFE_ERROR_NAMES for cls in type(exc).__mro__):
        return f"{type(exc).__name__}: {exc}"
    return "InternalError: operation failed"


def _tighten_file_mode(path: Path) -> None:
    """Best-effort ``chmod 0600``; tolerated to no-op where the FS or OS
    ignores POSIX modes (e.g. some Windows filesystems). A symlink is never
    followed - the nonce-bearing outbox must not retarget another file."""
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            return
        os.chmod(path, 0o600)
    except OSError:
        pass


def _outbox_line_expired(line: str, cutoff: float) -> bool:
    """True only for a well-formed record line whose ``ts`` predates the
    retention horizon. Lines that do not parse (missing/invalid ``ts``,
    non-JSON) are kept - the outbox may hold data this transport did not
    write, and pruning must not silently drop it."""
    try:
        record = json.loads(line)
    except ValueError:
        return False
    stamp = record.get("ts") if isinstance(record, dict) else None
    if not isinstance(stamp, str):
        return False
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp() < cutoff


def _prune_outbox(path: Path, *, stderr: Any = None) -> None:
    """Best-effort TTL prune of the outbox after a successful append.

    Reads the file (``O_NOFOLLOW`` where available so a swapped-in symlink
    is not followed) and rewrites it via a sibling temp file created 0600
    + ``O_NOFOLLOW``; ``os.replace`` means the existing destination keeps
    working throughout. Any failure is reported on stderr and swallowed —
    the nonce was already delivered, so a prune error must never fail the
    send.
    """
    target = sys.stderr if stderr is None else stderr
    tmp = path.parent / f"{path.name}.{os.getpid()}.tmp"
    try:
        cutoff = (
            datetime.now(timezone.utc).timestamp() - _OUTBOX_RETAIN_SECONDS
        )
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        dropped = 0
        kept: list[str] = []
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            for line in handle:
                if _outbox_line_expired(line, cutoff):
                    dropped += 1
                else:
                    kept.append(line)
        if not dropped:
            return
        flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(tmp), flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.writelines(kept)
            _tighten_file_mode(tmp)
            os.replace(tmp, path)
            _tighten_file_mode(path)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
    except OSError as exc:
        print(f"[{SERVER_NAME}] outbox prune skipped: {exc}", file=target)


def build_parser() -> argparse.ArgumentParser:
    """CLI for the stdio server: fixed operator identity + data dir."""
    parser = argparse.ArgumentParser(
        prog="agent-personal-memory-mcp",
        description=(
            "Run the agent-personal-memory MCP server over stdio. The five "
            "binding flags pin the fixed operator identity and session the "
            "confirm-gate is bound to; they cannot be supplied per tool "
            "call. stdout carries JSON-RPC only; diagnostics go to stderr."
        ),
        epilog=(
            "Deletion previews are delivered to the operator outbox "
            "<data_dir>/confirmations.jsonl (plus a stderr notice); the "
            "one-time nonce never appears in a tool result. The operator "
            "reads the nonce there and calls personal_memory_confirm."
        ),
    )
    parser.add_argument(
        "--actor-platform",
        help="(required) fixed operator platform label, e.g. 'cli'",
    )
    parser.add_argument(
        "--actor-user-id", help="(required) fixed operator user id"
    )
    parser.add_argument(
        "--actor-display-name", default="", help="operator display name"
    )
    parser.add_argument(
        "--session-key",
        help="(required) authenticated session key for the binding",
    )
    parser.add_argument(
        "--session-id", help="(required) session id for the binding"
    )
    parser.add_argument(
        "--chat-id", help="(required) chat/channel id the session is bound to"
    )
    parser.add_argument(
        "--thread-id", default="", help="optional thread id for the binding"
    )
    parser.add_argument(
        "--profile", default="", help="optional binding profile"
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help=(
            "memory store directory; falls back to PERSONAL_MEMORY_DATA_DIR, "
            "then the package default"
        ),
    )
    return parser


def build_binding(args: argparse.Namespace) -> apm.SessionBinding:
    """Build the fixed operator ``SessionBinding`` from parsed args.

    Raises ``ValueError`` on any missing required flag or on a binding the
    confirm-gate itself would reject - startup must fail fast instead of
    silently loosening confirmation binding.
    """
    values = {
        "platform": (args.actor_platform or "").strip(),
        "user_id": (args.actor_user_id or "").strip(),
        "session_key": (args.session_key or "").strip(),
        "session_id": (args.session_id or "").strip(),
        "chat_id": (args.chat_id or "").strip(),
        "thread_id": (args.thread_id or "").strip(),
        "profile": (args.profile or "").strip(),
        "display_name": (args.actor_display_name or "").strip(),
    }
    missing = [flag for flag, field in _BINDING_FLAGS if not values[field]]
    if missing:
        raise ValueError(
            "missing required startup flag(s): " + ", ".join(missing)
        )
    binding = apm.SessionBinding(**values)
    if not _binding_complete(binding):
        raise ValueError(
            "invalid operator binding: user_id must be a real user id "
            "(empty, '0', 'unknown', 'none' are rejected)"
        )
    return binding


def make_outbox_send(
    outbox_path: str | Path,
    *,
    request_id: str | None = None,
    stderr: Any = None,
) -> Callable[[str], bool]:
    """Operator-outbox ``send(text) -> bool`` transport for the confirm-gate.

    Appends one JSON line ``{"ts", "request_id"?, "body"}`` to
    ``outbox_path`` and prints a short notice to stderr (the nonce-bearing
    body is never printed). A failed write raises so ``open_preview``
    denies nonce registration - the send fails closed. After a successful
    append the file is pruned: lines whose ``ts`` has passed the nonce TTL
    horizon are dropped (see ``_prune_outbox``), keeping the outbox a
    transient delivery channel rather than a growing plaintext log.
    """
    path = Path(outbox_path)

    def send(text: str) -> bool:
        os.makedirs(str(path.parent), mode=0o700, exist_ok=True)
        record: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "body": text,
        }
        if request_id:
            record["request_id"] = str(request_id)
        # The outbox holds plaintext preview + a live nonce: keep it at
        # 0600 custody — tighten a pre-existing loose file, create with
        # 0600, then tighten again in case the file was recreated between.
        _tighten_file_mode(path)
        flags = os.O_CREAT | os.O_WRONLY | os.O_APPEND
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(path), flags, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        _tighten_file_mode(path)
        # The append succeeded (the nonce is delivered): prune dead
        # nonce-bearing lines so plaintext previews do not accumulate.
        _prune_outbox(path, stderr=stderr)
        target = sys.stderr if stderr is None else stderr
        suffix = f" for request {request_id}" if request_id else ""
        print(
            f"[{SERVER_NAME}] deletion preview{suffix} delivered to the "
            f"operator outbox ({OUTBOX_NAME}); confirm via "
            f"personal_memory_confirm",
            file=target,
        )
        return True

    return send


def _scrub_nonce(value: Any) -> Any:
    """Drop nonce-shaped keys from a payload (defense in depth)."""
    if isinstance(value, dict):
        return {
            key: _scrub_nonce(item)
            for key, item in value.items()
            if key not in _NONCE_KEYS
        }
    if isinstance(value, list):
        return [_scrub_nonce(item) for item in value]
    return value


def _decode_blob(value: bytes | bytearray) -> Any:
    """Decode a decrypted blob for the wire: utf-8 text, else a base64
    record of the same shape as ``store._export_bytes``."""
    raw = bytes(value)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "encoding": "base64",
            "data": base64.b64encode(raw).decode("ascii"),
        }


def _normalize_nested(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return _decode_blob(value)
    if isinstance(value, dict):
        return {key: _normalize_nested(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_nested(item) for item in value]
    return value


def _normalize_payload(result: Any) -> Any:
    """Coerce owner-flow returns into JSON-RPC-safe values.

    Decrypted claim ``content`` and source ``snapshot`` fields are raw
    ``bytes`` nested inside result dicts and lists, so the whole structure
    is walked: every blob crosses the wire as utf-8 text, or as a base64
    record when the payload is not utf-8. Keys and non-bytes values are
    preserved untouched.
    """
    if isinstance(result, (bytes, bytearray)):
        # Top-level bytes are export_owner_data's utf-8 JSON document.
        try:
            result = json.loads(bytes(result).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _decode_blob(result)
    return _normalize_nested(result)


class McpToolSurface:
    """Fixed-identity dispatch of the canonical ``TOOL_SCHEMAS`` surface.

    The startup-provisioned actor and session binding are bound into every
    call. Arguments are validated against the canonical schemas: unknown
    tools, missing required fields, and any extra field - including
    injected identity selectors - are rejected.
    """

    def __init__(
        self,
        *,
        actor: apm.Actor,
        binding: apm.SessionBinding,
        store: PersonalMemoryStore,
        outbox_path: str | Path,
        stderr: Any = None,
    ) -> None:
        self._actor = actor
        self._binding = binding
        self._store = store
        self._outbox_path = Path(outbox_path)
        self._stderr = stderr

    @property
    def actor(self) -> apm.Actor:
        return self._actor

    @property
    def binding(self) -> apm.SessionBinding:
        return self._binding

    @property
    def store(self) -> PersonalMemoryStore:
        return self._store

    @property
    def outbox_path(self) -> Path:
        return self._outbox_path

    def tool_schemas(self) -> list[dict[str, Any]]:
        """MCP ``tools/list`` entries mirroring ``TOOL_SCHEMAS`` verbatim."""
        return [
            {
                "name": schema["name"],
                "description": schema["description"],
                "inputSchema": schema["parameters"],
            }
            for schema in apm.TOOL_SCHEMAS.values()
        ]

    def call_tool(self, name: str, arguments: Any = None) -> Any:
        """Dispatch one tool call bound to the fixed identity."""
        args = self._validate_arguments(name, arguments)
        if name == "personal_memory_confirm":
            return self._call_confirm(args)
        if name in _PREVIEW_TOOLS:
            return self._call_preview(name, args)
        return self._call_owner(name, args)

    # --- internals -------------------------------------------------------

    def _validate_arguments(self, name: str, arguments: Any) -> dict[str, Any]:
        schema = apm.TOOL_SCHEMAS.get(name)
        if schema is None:
            raise ValidationError(f"unknown tool: {name}")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ValidationError("arguments: must be a JSON object")
        properties = schema.get("parameters", {}).get("properties", {})
        extra = sorted(set(arguments) - set(properties))
        if extra:
            raise ValidationError(
                "arguments: unexpected field(s): " + ", ".join(extra)
            )
        missing = [
            field
            for field in schema.get("parameters", {}).get("required", [])
            if field not in arguments
        ]
        if missing:
            raise ValidationError(
                "arguments: missing required field(s): " + ", ".join(missing)
            )
        return dict(arguments)

    def _call_owner(self, name: str, args: dict[str, Any]) -> Any:
        func = getattr(apm, name, None)
        if func is None or name == "personal_memory_confirm":
            raise ValidationError(f"tool has no dispatch target: {name}")
        kwargs = dict(args)
        kwargs[_STORE_KWARG.get(name, "store")] = self._store
        return _scrub_nonce(_normalize_payload(func(self._actor, **kwargs)))

    def _call_preview(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        func = getattr(apm, name)
        kwargs = dict(args)
        kwargs["store"] = self._store
        kwargs["session_binding"] = self._binding
        payload = _scrub_nonce(_normalize_payload(func(self._actor, **kwargs)))
        request_id = (
            payload.get("request_id") if isinstance(payload, dict) else None
        )
        if request_id:
            # Fold the user "open preview" step into the preview call: the
            # nonce-bearing text is delivered to the operator outbox now;
            # the tool result keeps only non-authorizing fields.
            send = make_outbox_send(
                self._outbox_path,
                request_id=str(request_id),
                stderr=self._stderr,
            )
            apm.CONFIRM_REGISTRY.open_preview(
                str(request_id), self._binding, send=send
            )
            payload["delivery"] = "operator_outbox"
        return payload

    def _call_confirm(self, args: dict[str, Any]) -> dict[str, Any]:
        nonce = args["nonce"]
        if not isinstance(nonce, str) or not nonce.strip():
            raise ValidationError("nonce: must be a non-empty string")
        record = apm.CONFIRM_REGISTRY.consume_confirm(
            nonce.strip(), self._binding
        )
        return _scrub_nonce(
            _normalize_payload(
                apm.consume_delete_request(record, store=self._store)
            )
        )


def build_surface(args: argparse.Namespace) -> McpToolSurface:
    """Provision the fixed identity, store, and outbox for one server run."""
    binding = build_binding(args)
    store = PersonalMemoryStore(data_dir=args.data_dir or None)
    return McpToolSurface(
        actor=binding.actor(),
        binding=binding,
        store=store,
        outbox_path=store.data_dir / OUTBOX_NAME,
    )


def _load_sdk() -> tuple[Any, Any, Any]:
    """Import the optional MCP SDK lazily; ImportError when absent."""
    from mcp import types
    from mcp.server import Server
    from mcp.server.stdio import stdio_server

    return types, Server, stdio_server


def build_sdk_server(surface: McpToolSurface) -> Any:
    """Adapt the surface to the installed ``mcp`` SDK (1.x and 2.x APIs)."""
    types, Server, _stdio_server = _load_sdk()
    tool_defs = surface.tool_schemas()

    def _tool_objects() -> list[Any]:
        return [
            types.Tool(
                name=entry["name"],
                description=entry["description"],
                inputSchema=entry["inputSchema"],
            )
            for entry in tool_defs
        ]

    if hasattr(Server, "call_tool"):
        # mcp 1.x: decorator registration. A dict return becomes structured
        # content plus a JSON text block; a raised exception becomes an
        # isError result whose text is str(exc) — so route it through
        # _error_text to keep unexpected internals out of the tool result.
        server = Server(SERVER_NAME, version=apm.__version__)

        @server.list_tools()
        async def _list_tools() -> list[Any]:
            return _tool_objects()

        @server.call_tool()
        async def _call_tool(name: str, arguments: Any = None) -> Any:
            try:
                payload = surface.call_tool(name, arguments or {})
            except Exception as exc:
                raise RuntimeError(_error_text(exc)) from exc
            if isinstance(payload, dict):
                return payload
            return [
                types.TextContent(
                    type="text",
                    text=json.dumps(payload, ensure_ascii=False),
                )
            ]

        return server

    # mcp 2.x: constructor-registered handlers returning result models.
    async def _on_list_tools(_ctx: Any, _params: Any) -> Any:
        return types.ListToolsResult(tools=_tool_objects())

    async def _on_call_tool(_ctx: Any, params: Any) -> Any:
        try:
            payload = surface.call_tool(params.name, params.arguments or {})
        except Exception as exc:
            return types.CallToolResult(
                content=[
                    types.TextContent(type="text", text=_error_text(exc))
                ],
                isError=True,
            )
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=json.dumps(payload, ensure_ascii=False),
                )
            ],
            structuredContent=payload if isinstance(payload, dict) else None,
            isError=False,
        )

    return Server(
        SERVER_NAME,
        version=apm.__version__,
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
    )


async def _serve(surface: McpToolSurface) -> None:
    _types, _server_cls, stdio_server = _load_sdk()
    server = build_sdk_server(surface)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _load_sdk()
    except ImportError:
        print(f"{SERVER_NAME}: {INSTALL_HINT}", file=sys.stderr)
        return 2
    try:
        surface = build_surface(args)
    except ValueError as exc:
        parser.error(str(exc))
    print(
        f"[{SERVER_NAME}] serving {len(apm.TOOL_SCHEMAS)} tools on stdio; "
        f"data dir {surface.store.data_dir}; outbox {surface.outbox_path}",
        file=sys.stderr,
    )
    try:
        asyncio.run(_serve(surface))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

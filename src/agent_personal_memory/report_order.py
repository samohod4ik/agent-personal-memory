"""Versioned, BB-only policy for the order of failed-test reports."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from itertools import groupby

from .journal import _evidence_source_on
from .lifecycle import MemoryState
from .store import NotFoundError, utcnow_iso
from .validation import ValidationError

POLICY = "auto_core_report_order"
CONTENT = {
    "command_first": "For failed test reports, put the reproduction command before the diagnosis.",
    "cause_first": "For failed test reports, put the diagnosis before the reproduction command.",
}
KINDS = {*CONTENT, "exception", "ambiguous"}
_EN_CONTEXT = r"(?:for|in) (?:failed[- ]test reports|reports (?:on|about) failed tests)"
_RU_CONTEXT = r"в отч[её]тах (?:об упавших тестах|о падении тестов)"
_EN_FIRST = r"(?P<verb>start with|put) (?:the )?(?P<direction>reproduction command|command|diagnosis|cause)(?P<first> first)?"
_RU_FIRST = r"(?:начинай|начни) с (?P<ru_direction>причины|команды(?: запуска| воспроизведения)?)"
_CHANGE = re.compile(rf"(?:{_EN_CONTEXT},? (?:now|from now on) {_EN_FIRST}|{_RU_CONTEXT} теперь {_RU_FIRST})")
_EN_REVOKE = r"(?:stop putting|no longer put|don't put|do not put) (?:the )?(?:reproduction command|command|diagnosis|cause) first"
_RU_REVOKE = r"больше не (?:ставь|показывай|давай) (?:команду|причину)(?: запуска| воспроизведения)? перв(?:ой|ым)"
_REVOKE = re.compile(rf"(?:{_EN_REVOKE} {_EN_CONTEXT}|{_RU_REVOKE} {_RU_CONTEXT})")
_SHORT_CHANGE = re.compile(rf"(?:(?:now|from now on) {_EN_FIRST}|теперь {_RU_FIRST})")
_SHORT_REVOKE = re.compile(rf"(?:{_EN_REVOKE}|{_RU_REVOKE})")
_SHORT_CHALLENGE = re.compile(
    r"(?:(?:change|fix|reconsider) (?:the )?(?:order|ordering)|"
    r"(?:нужно|надо) (?:исправить|изменить|пересмотреть) порядок)"
)
_CHALLENGE = re.compile(
    rf"(?:(?:change|reconsider|fix) (?:the )?(?:order|ordering) {_EN_CONTEXT}|"
    rf"{_RU_CONTEXT} (?:нужно|надо) (?:исправить|изменить|пересмотреть) порядок)"
)
_DISQUALIFIED = re.compile(
    r"[\"«»“”`]|(?:^|\n)\s*>|^\s*(?:сейчас|сегодня|this time|today|for this report)\b|"
    r"\b(?:only (?:now|this time|for this (?:run|project))|just (?:this once|this time)|"
    r"for this project|in this project|this time only|as quoted|someone said|"
    r"только (?:сейчас|сегодня|для этого проекта|в этом проекте|в этот раз)|"
    r"лишь (?:сейчас|сегодня|в этот раз)|в этот раз|на этот раз|для этого проекта|в этом проекте|"
    r"это цитата|он сказал|она сказала)\b",
    re.IGNORECASE,
)
_NAMED_REPORT = re.compile(
    r"\b(?:failed[- ]test reports?|reports? (?:on|about|for) (?:failing|failed) tests|"
    r"отч[её]т\w* (?:об упавших тестах|о падении тестов))\b", re.IGNORECASE,
)
_NAMED_ORDER = re.compile(
    r"\b(?:first|before|after|precede|followed by|start with|lead with|"
    r"сначала|сперва|первой|первым|перед|после|начин\w* с)\b", re.IGNORECASE,
)
_NAMED_DIRECTION = re.compile(r"\b(?:command|diagnosis|cause|команд\w*|причин\w*)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Proof:
    source_id: str
    version: int
    start: int
    end: int
    task: str
    occurred_at: str
    expires_at: str


def _correction(text, task_context=False):
    statement = " ".join(text.casefold().split()).removesuffix(".")
    match = _CHANGE.fullmatch(statement)
    if not match and task_context:
        match = _SHORT_CHANGE.fullmatch(statement)
    if match:
        if match["verb"] == "put" and not match["first"]:
            return True, None
        value = match["direction"] or match["ru_direction"]
        return True, ("command_first" if value.startswith(("command", "reproduction", "команд")) else "cause_first")
    return bool(_REVOKE.fullmatch(statement) or _CHALLENGE.fullmatch(statement)
                or (task_context and (_SHORT_REVOKE.fullmatch(statement)
                                      or _SHORT_CHALLENGE.fullmatch(statement)))), None


def eligible(task, text):
    # An explicit change or revocation without authenticated context cannot be
    # recovered as a vote by a less constrained classifier.
    return bool(task and not _DISQUALIFIED.search(text) and not _correction(text, task_context=True)[0])


def names_slot(text):
    return bool(_NAMED_REPORT.search(text) and _NAMED_ORDER.search(text) and _NAMED_DIRECTION.search(text))


def _proof(store, conn, tenant, source_id, version, start, end):
    event, quote = _evidence_source_on(
        store, conn, tenant, source_id, version, start, end,
        require_unredacted=True, include_report_order_context=True,
    )
    task = event.get("report_order_task_id")
    if (not task or event["role"] != "user" or event["origin"] != "direct"
            or not event.get("whole_owner_turn") or event["attachments"]
            or start != 0 or quote != event["text"]):
        raise ValidationError("report order: exact attested owner turn required")
    return Proof(source_id, version, start, end, task, event["occurred_at"], event["expires_at"])


def observations_on(store, conn, tenant, observations, events):
    """Validate all model observations before any votes can be written."""
    if not isinstance(observations, list) or len(observations) > 20:
        raise ValidationError("report order: at most 20 observations")
    allowed = {
        (event["source_id"], event["version"]): event
        for event in events if event.get("report_order_eligible")
    }
    seen = set()
    result = []
    for observation in observations:
        if (not isinstance(observation, dict)
                or set(observation) != {"source_id", "source_version", "start", "end", "kind"}):
            raise ValidationError("report order: invalid observation fields")
        source_id, version, start, end, kind = (
            observation[key] for key in ("source_id", "source_version", "start", "end", "kind")
        )
        if not isinstance(source_id, str) or type(version) is not int or not isinstance(kind, str) or kind not in KINDS:
            raise ValidationError("report order: invalid observation")
        key = (source_id, version)
        if key not in allowed or key in seen:
            raise ValidationError("report order: source outside batch or duplicate")
        seen.add(key)
        proof = _proof(store, conn, tenant, source_id, version, start, end)
        if kind in CONTENT:
            result.append((proof, kind))
    return result


def _slot(conn, tenant):
    return conn.execute("SELECT * FROM memory_report_order_slots WHERE tenant_id=?", (tenant,)).fetchone()


def last_override_on(conn, tenant):
    row = _slot(conn, tenant)
    return row["direct_at"] if row else None


def _set_slot(store, conn, tenant, slot, direction, cutoff, proof, *, action_source=None):
    claim_id = slot["claim_id"] if slot else None
    claim = store._lookup_claim(conn, tenant, claim_id) if claim_id else None
    now = utcnow_iso()
    version = None
    if direction is not None or (claim and claim["deleted_at"] is None):
        if claim is None or claim["deleted_at"] is not None:
            claim_id = str(uuid.uuid4())
            conn.execute("INSERT INTO memory_claims(id,tenant_id,claim_kind,created_at) VALUES(?,?,?,?)",
                         (claim_id, tenant, "preference", now))
            previous = None
        else:
            previous = store._newest_claim_version(conn, tenant, claim_id)
        version = previous["version"] + 1 if previous else 1
        if previous and previous["state"] == "confirmed":
            conn.execute("UPDATE memory_claim_versions SET state='superseded' WHERE id=?", (previous["id"],))
        status = MemoryState.CONFIRMED if direction else MemoryState.RETRACTED
        version_id = store._insert_claim_version(
            conn, tenant_id=tenant, claim_id=claim_id, version=version, state=status, sensitivity="normal",
            content=CONTENT.get(direction, "").encode(), confidence=1.0,
            explanation="Attested failed-test report order evidence", valid_from=None, valid_to=None,
            expires_at=min((item.expires_at for item in proof), default=None),
            supersedes_id=previous["id"] if previous else None, now=now, change_reason=POLICY,
        )
        conn.executemany(
            "INSERT INTO memory_evidence VALUES(?,?,?,?,?,?,?)",
            [(tenant, claim_id, version, item.source_id, item.version, item.start, item.end) for item in proof],
        )
        store._append_audit(
            conn, tenant_id=tenant, actor_id=tenant,
            action="claim.confirm" if direction else "claim.retract", target_kind="claim_version",
            target_id=version_id, outcome=status.value, policy_reason=POLICY,
        )
    direct_at = cutoff if action_source else (slot["direct_at"] if slot else None)
    conn.execute("""INSERT INTO memory_report_order_slots VALUES(?,?,?,?,?,?)
        ON CONFLICT(tenant_id) DO UPDATE SET claim_id=excluded.claim_id,direction=excluded.direction,
        cutoff_at=excluded.cutoff_at,action_source=excluded.action_source,direct_at=excluded.direct_at""",
        (tenant, claim_id, direction, cutoff, action_source, direct_at))
    conn.execute("""DELETE FROM memory_report_order_votes
        WHERE tenant_id=? AND source_id IN (
            SELECT source_id FROM memory_journal
            WHERE tenant_id=? AND (redacted=1 OR occurred_at<=? OR expires_at<=?))""",
        (tenant, tenant, cutoff, now))
    return {"value": direction, "claim_id": claim_id, "version": version}


def record_correction_on(store, conn, tenant, source_id, version, occurred_at, text, *, task_context):
    changed, direction = _correction(text, task_context)
    if not changed or store._memory_mode_on(conn, tenant) != "auto_insight":
        return None
    slot = _slot(conn, tenant)
    if slot and occurred_at < slot["cutoff_at"]:
        return None
    if (slot and occurred_at == slot["cutoff_at"] and slot["action_source"]
            and slot["action_source"] != source_id and slot["direction"] != direction):
        direction = None  # Same-second incompatible explicit decisions fail closed.
    evidence = [_proof(store, conn, tenant, source_id, version, 0, len(text))]
    return _set_slot(store, conn, tenant, slot, direction, occurred_at, evidence, action_source=source_id)


def visible_on(store, conn, tenant, row, evidence):
    if row["change_reason"] != POLICY or store._memory_mode_on(conn, tenant) != "auto_insight":
        return False
    slot = _slot(conn, tenant)
    if not slot or slot["claim_id"] != row["claim_id"] or not slot["direction"]:
        return False
    tasks = set()
    for item in evidence:
        try:
            proof = _proof(store, conn, tenant, item["source_id"], item["source_version"],
                           item["span_start"], item["span_end"])
        except (NotFoundError, ValidationError):
            return False
        tasks.add(proof.task)
    return len(tasks) >= (1 if slot["action_source"] else 2)


def apply_observations_on(store, conn, tenant, observations):
    slot = _slot(conn, tenant)
    cutoff = slot["cutoff_at"] if slot else ""
    conn.executemany("""INSERT OR IGNORE INTO memory_report_order_votes VALUES(?,?,?,?,?,?)""",
                     [(tenant, p.source_id, p.version, p.start, p.end, direction)
                      for p, direction in observations if p.occurred_at > cutoff])
    if not observations:
        return None
    direction = slot["direction"] if slot else None
    live = False
    if direction:
        claim = store._newest_claim_version(conn, tenant, slot["claim_id"])
        if claim and claim["state"] == "confirmed":
            current = conn.execute(
                "SELECT * FROM memory_evidence WHERE tenant_id=? AND claim_id=? AND claim_version=?",
                (tenant, slot["claim_id"], claim["version"]),
            ).fetchall()
            live = visible_on(store, conn, tenant, {**dict(claim), "claim_id": slot["claim_id"]}, current)
    ordered = []
    votes = conn.execute("""SELECT v.* FROM memory_report_order_votes v
        JOIN memory_journal j ON j.tenant_id=v.tenant_id AND j.source_id=v.source_id
        WHERE v.tenant_id=? AND j.redacted=0 AND j.occurred_at>? AND j.expires_at>?
        ORDER BY j.occurred_at, v.source_id""", (tenant, cutoff, utcnow_iso())).fetchall()
    for row in votes:
        try:
            proof = _proof(store, conn, tenant, row["source_id"], row["source_version"],
                           row["span_start"], row["span_end"])
        except (NotFoundError, ValidationError):
            continue
        ordered.append((proof, row["direction"]))
    tasks = {}
    decision = None
    for occurred_at, group in groupby(ordered, key=lambda vote: vote[0].occurred_at):
        simultaneous = {}
        for proof, kind in group:
            prior = simultaneous.setdefault(proof.task, (proof, kind))
            if prior[1] != kind:
                simultaneous[proof.task] = (proof, None)
        tasks.update(simultaneous)
        ready = {}
        for kind in CONTENT:
            if live and kind == direction:
                continue
            matching = [proof for proof, vote in tasks.values() if vote == kind]
            if len(matching) >= 2:
                ready[kind] = matching[:2]
        if ready:
            # A batch containing a full reversal must behave as it would if
            # the same turns arrived separately. Tied series prove no value.
            direction, evidence = next(iter(ready.items())) if len(ready) == 1 else (None, [])
            decision = _set_slot(store, conn, tenant, slot, direction, occurred_at, evidence)
            slot = _slot(conn, tenant)
            live = direction is not None
            tasks.clear()
    return decision


def forget_claim_on(conn, tenant, claim_id, now):
    conn.execute("""UPDATE memory_report_order_slots
        SET direction=NULL,cutoff_at=MAX(cutoff_at,?),action_source=NULL
        WHERE tenant_id=? AND claim_id=?""", (now, tenant, claim_id))

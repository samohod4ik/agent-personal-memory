"""Bilingual user-facing labels for personal-memory owner flows."""

from __future__ import annotations

from .validation import ValidationError

SUPPORTED_LANGUAGES = frozenset({"en", "ru"})

_STATE_LABELS = {
    "en": {
        "candidate": "candidate memory",
        "confirmed": "confirmed memory",
    },
    "ru": {
        "candidate": "кандидат в память",
        "confirmed": "подтверждённая память",
    },
}

_KIND_LABELS = {
    "en": {
        "preference": "preference",
        "confirmed_fact": "confirmed fact",
        "candidate_insight": "candidate insight",
        "raw_event": "raw event",
        "task": "task",
        "decision": "decision",
        "summary": "summary",
    },
    "ru": {
        "preference": "предпочтение",
        "confirmed_fact": "подтверждённый факт",
        "candidate_insight": "кандидат-вывод",
        "raw_event": "исходное событие",
        "task": "задача",
        "decision": "решение",
        "summary": "краткое резюме",
    },
}

_SENSITIVITY_LABELS = {
    "en": {
        "normal": "normal sensitivity",
        "sensitive": "sensitive",
        "restricted": "restricted",
    },
    "ru": {
        "normal": "обычная чувствительность",
        "sensitive": "чувствительные данные",
        "restricted": "ограниченные данные",
    },
}

_LIST_INTRO = {
    "en": "Personal memories for the current session owner.",
    "ru": "Личная память текущего пользователя сессии.",
}

_CANDIDATE_LIST_INTRO = {
    "en": "Candidate memories awaiting your review.",
    "ru": "Кандидаты в память, ожидающие вашего решения.",
}

_EXPORT_HEADING = {
    "en": "Personal memory export for the current session owner.",
    "ru": "Экспорт личной памяти текущего пользователя сессии.",
}

_EXPLAIN_INTRO = {
    "en": "This item is remembered because",
    "ru": "Эта запись сохранена, потому что",
}

_SOURCE_PREFIX = {
    "en": "sources",
    "ru": "источники",
}

_MUTATION_CONFIRM = {
    "correct": {
        "en": "Memory updated with your correction (version {version}).",
        "ru": "Память обновлена с вашим исправлением (версия {version}).",
    },
    "forget_preview": {
        "en": "Preview: forget {summary}. Open /memory_delete to review the exact list.",
        "ru": "Предпросмотр: удалить {summary}. Откройте /memory_delete для точного списка.",
    },
    "forget": {
        "en": "The selected memory was permanently forgotten.",
        "ru": "Выбранная запись памяти удалена без возможности восстановления.",
    },
    "do_not_remember_preview": {
        "en": "Preview: forget {count} matching {unit}. Open /memory_delete to review the exact list.",
        "ru": "Предпросмотр: удалить записей — {count}. Откройте /memory_delete для точного списка.",
    },
    "do_not_remember_none": {
        "en": "No matching memories were found to forget.",
        "ru": "Подходящие записи памяти для удаления не найдены.",
    },
    "do_not_remember_some": {
        "en": "Forgot {count} matching {unit}.",
        "ru": "Удалено записей памяти: {count}.",
    },
    "confirm_candidate": {
        "en": "Candidate memory confirmed and saved.",
        "ru": "Кандидат в память подтверждён и сохранён.",
    },
    "reconcile_accept": {
        "en": "Done: the new fact replaced the old one; the old one is kept in history.",
        "ru": "Готово: новый факт заменил прежний, прежний сохранён в истории.",
    },
    "reconcile_reject": {
        "en": "Okay, I kept the previous fact.",
        "ru": "Хорошо, оставил прежний факт.",
    },
    "reject_candidate_preview": {
        "en": "Preview: reject {summary}. Open /memory_delete to review the exact list.",
        "ru": "Предпросмотр: отклонить {summary}. Откройте /memory_delete для точного списка.",
    },
    "reject_candidate": {
        "en": "Candidate memory rejected and permanently erased.",
        "ru": "Кандидат в память отклонён и удалён без возможности восстановления.",
    },
    "remember_confirmed": {
        "en": "Remembered and confirmed for future turns.",
        "ru": "Запомнено и подтверждено для следующих ответов.",
    },
    "remember_candidate": {
        "en": "Saved as a candidate. Confirm it to use on later turns.",
        "ru": "Сохранено как кандидат. Подтвердите, чтобы использовать в следующих ответах.",
    },
    "memory_mode": {
        "en": "Personal memory mode is {mode}.",
        "ru": "Режим личной памяти: {mode}.",
    },
}


def normalize_language(value: object) -> str:
    if value is None:
        return "en"
    if not isinstance(value, str):
        _fail_language()
    language = value.strip().lower()
    if not language:
        return "en"
    if language not in SUPPORTED_LANGUAGES:
        _fail_language()
    return language


def _fail_language() -> None:
    raise ValidationError("language: must be 'en' or 'ru'")


def _label(table: dict[str, dict[str, str]], language: str, key: object, fallback: str) -> str:
    return table.get(language, table["en"]).get(str(key), fallback)


def format_claim_summary(
    row: dict[str, object],
    *,
    language: str,
    include_content: bool = False,
) -> str:
    lang = normalize_language(language)
    state = _label(_STATE_LABELS, lang, row.get("state"), str(row.get("state", "")))
    kind = _label(_KIND_LABELS, lang, row.get("claim_kind"), str(row.get("claim_kind", "")))
    confidence = float(row.get("confidence", 0.0))
    created_at = str(row.get("created_at", ""))
    if lang == "ru":
        base = f"{kind} ({state}), уверенность {confidence:.2f}, создано {created_at}"
    else:
        base = f"{kind} ({state}), confidence {confidence:.2f}, created {created_at}"
    if include_content and row.get("content") is not None:
        content = row["content"]
        if isinstance(content, (bytes, bytearray)):
            try:
                text = bytes(content).decode("utf-8")
            except UnicodeDecodeError:
                text = "<binary>"
        else:
            text = str(content)
        if lang == "ru":
            return f"{base}; содержание: {text}"
        return f"{base}; content: {text}"
    return base


def format_list_heading(language: str) -> str:
    return _LIST_INTRO[normalize_language(language)]


def format_candidate_list_heading(language: str) -> str:
    return _CANDIDATE_LIST_INTRO[normalize_language(language)]


def format_export_heading(language: str) -> str:
    return _EXPORT_HEADING[normalize_language(language)]


def format_export_summary(
    language: str,
    *,
    sources: int,
    claims: int,
    deletion_receipts: int,
) -> str:
    lang = normalize_language(language)
    if lang == "ru":
        return (
            f"Источников: {sources}, записей памяти: {claims}, "
            f"квитанций удаления: {deletion_receipts}."
        )
    return f"Sources: {sources}, claims: {claims}, deletion receipts: {deletion_receipts}."


def format_explain_narrative(payload: dict[str, object], *, language: str) -> str:
    lang = normalize_language(language)
    state = _label(_STATE_LABELS, lang, payload.get("state"), str(payload.get("state", "")))
    kind = _label(_KIND_LABELS, lang, payload.get("claim_kind"), str(payload.get("claim_kind", "")))
    confidence = float(payload.get("confidence", 0.0))
    created_at = str(payload.get("created_at", ""))
    expires_at = payload.get("expires_at")
    sources = payload.get("sources") or []
    source_kinds = sorted(
        {
            str(item.get("source_kind", ""))
            for item in sources
            if isinstance(item, dict) and item.get("source_kind")
        }
    )
    source_text = ", ".join(source_kinds) if source_kinds else ("none" if lang == "en" else "нет")
    intro = _EXPLAIN_INTRO[lang]
    prefix = _SOURCE_PREFIX[lang]
    if lang == "ru":
        expiry = f", действует до {expires_at}" if expires_at else ""
        return (
            f"{intro} это {kind} со статусом «{state}», уверенность {confidence:.2f}, "
            f"создано {created_at}{expiry}; {prefix}: {source_text}."
        )
    expiry = f", valid until {expires_at}" if expires_at else ""
    return (
        f"{intro} it is a {kind} marked as {state} with confidence {confidence:.2f}, "
        f"created {created_at}{expiry}; {prefix}: {source_text}."
    )


def format_mutation_confirmation(kind: str, *, language: str, **values: object) -> str:
    lang = normalize_language(language)
    templates = _MUTATION_CONFIRM[kind]
    template = templates.get(lang, templates["en"])
    if kind in {"do_not_remember_some", "do_not_remember_preview"}:
        count = int(values.get("count", 0))
        if lang == "en":
            unit = "memory" if count == 1 else "memories"
            return template.format(count=count, unit=unit)
        return template.format(count=count)
    return template.format(**values)

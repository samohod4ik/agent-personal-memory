# agent-personal-memory

Локальная зашифрованная персональная память для ИИ-агентов — библиотека на
Python, опциональный MCP-сервер (stdio) и Agent Skill. Лицензия MIT.

Канонический README на английском: [README.md](README.md)

## Что это

`agent_personal_memory` — хранилище памяти одного владельца, которое хост
агента встраивает напрямую. Данные живут в одном каталоге (`<data_dir>`):
база SQLite (`personal_memory.db`, WAL + `secure_delete`) и файл KEK на 256
бит (`master.key`, создаётся с правами 0600). Никакого сервера, сетевых
вызовов и телеметрии.

- **Шифрование на диске.** Каждое сохранённое поле (содержимое утверждения,
  объяснение, снапшот/локатор/метаданные источника) зашифровано AES-256-GCM
  свежим случайным DEK на запись, обёрнутым файловым KEK. Шифротекст
  привязан через AAD к tenant / entity / version / field, поэтому перенос
  блоба между записями ломает аутентифицированную расшифровку.
  `external_ref` источников хранятся как keyed HMAC-хэши.
- **Явная идентичность.** Каждый вызов принимает `Actor(platform, user_id,
  display_name)`, построенный хостом; ничего не читается из окружения
  сессии. Межтенантные чтения fail-closed: отсутствующие, чужие и удалённые
  записи возвращают одинаковый `NotFoundError`.
- **Деструктивные операции через подтверждение.** Забывание двухфазное:
  вызов со стороны модели создаёт только неавторизующий предпросмотр;
  одноразовый nonce генерируется внутри гейта, доставляется по инжектированному
  операторскому каналу и привязан к точному `SessionBinding` с TTL.
  Переданный моделью флаг `confirm` отклоняется.
- **Физическое удаление.** Подтверждённое удаление уничтожает обёрнутые DEK
  и шифрованные блобы, убирает рёбра поддержки и строки FTS, уплотняет базу
  и оставляет только обезличенный tombstone-рецепт.
- **Детерминированный recall.** SQLite FTS5 (тенант-фильтр внутри `MATCH`)
  плюс лексико-сущностное ранжирование и tenant-local BM25 — без
  эмбеддингов и без LLM в цикле.
- **Объяснимость и экспорт.** `explain_claim` / `explain_why_remembered`
  показывают происхождение, состояние жизненного цикла и опорные источники;
  `export_owner_data` делает полный экспорт владельца. Тексты для
  владельца — двуязычные (EN/RU).

См. [docs/threat-model.md](docs/threat-model.md) — что защищается, а что
нет, и [skills/agent-personal-memory/SKILL.md](skills/agent-personal-memory/SKILL.md)
— гайд по встраиванию для агентов.

## Установка

Требуется Python 3.10+. Единственная runtime-зависимость — `cryptography>=42`.

```bash
pip install .            # из checkout
pip install ".[mcp]"     # с опциональным MCP-сервером
# или напрямую из (приватного) репозитория при наличии доступа:
pip install "git+ssh://git@github.com/samohod4ik/agent-personal-memory.git"
```

## Быстрый старт (< 15 минут)

Сниппет ниже исполняется дословно тестом `tests/test_readme_quickstart.py`.

```python
import re
import tempfile

from agent_personal_memory import (
    Actor,
    CONFIRM_REGISTRY,
    PersonalMemoryStore,
    SessionBinding,
    consume_delete_request,
    forget_personal_memory,
    list_personal_memories,
    remember_personal_insight,
)

# резолюция data_dir: явный аргумент -> PERSONAL_MEMORY_DATA_DIR ->
# ~/.agent-personal-memory
with tempfile.TemporaryDirectory(prefix="pm-quickstart-") as data_dir:
    store = PersonalMemoryStore(data_dir=data_dir)

    # Идентичность явная: Actor конструирует хост.
    owner = Actor(platform="cli", user_id="owner-1", display_name="Owner")

    # remember: в режиме auto_insight (по умолчанию) запись сохраняется подтверждённой.
    saved = remember_personal_insight(
        owner, "I prefer morning standups", store=store
    )
    claim_id = str(saved["claim_id"])

    # list: инвентарь для владельца.
    inventory = list_personal_memories(owner, store=store)
    assert inventory["count"] >= 1

    # forget, фаза 1: неавторизующий предпросмотр — request_id плюс
    # человекочитаемый текст подтверждения, nonce отсутствует.
    binding = SessionBinding(
        platform="cli",
        user_id="owner-1",
        session_key="cli:owner-1",
        session_id="session-1",
        chat_id="cli-direct",
    )
    preview = forget_personal_memory(
        owner, claim_id, store=store, session_binding=binding
    )
    assert preview["preview"]
    assert "nonce" not in preview

    # forget, фаза 2: доставка предпросмотра оператору через инжектированный
    # send(text) -> bool. Nonce генерируется внутри гейта, существует только
    # внутри payload отправки и регистрируется лишь после успешного send().
    delivered = []

    def send(text):
        delivered.append(text)  # реальный хост печатает это оператору
        return True

    CONFIRM_REGISTRY.open_preview(
        str(preview["request_id"]), binding, send=send
    )
    nonce = re.search(r"/memory_confirm (\S+)", delivered[-1]).group(1)

    # forget, фаза 3: только nonce оператора авторизует удаление.
    record = CONFIRM_REGISTRY.consume_confirm(nonce, binding)
    result = consume_delete_request(record, store=store)
    assert result["erased_claim_ids"] == [claim_id]
    print(result["confirmation"])
```

## Карта API

Все имена ниже импортируются из `agent_personal_memory` (каноническая
поверхность — `src/agent_personal_memory/__init__.py`).

| Область | Имя | Что делает |
| --- | --- | --- |
| Хранилище / идентичность | `PersonalMemoryStore(data_dir=None)` | Открывает или создаёт каталог хранилища, применяет миграции схемы, загружает или создаёт `master.key` |
| | `Actor(platform, user_id, display_name="")` | Неизменяемая тенант-идентичность; её строит хост, модель её не подаёт |
| | `create_candidate(store, actor, ...)` | Unbound `PersonalMemoryStore.create_candidate`; всегда создаёт черновик (candidate) |
| Запоминание / обучение | `remember_personal_insight` | Сохранить явный долговечный факт или предпочтение; запись падает в `confirmed` или `candidate` по режиму памяти владельца |
| | `learn_from_turn` | Правил-based извлечение долговечных инсайтов из реплики владельца (сохраняет только в режиме `auto_insight`) |
| | `get_memory_mode` / `set_memory_mode` | Режим памяти владельца: `auto_insight` (по умолчанию) или `confirm_required` |
| Ревью | `list_candidate_memories` | Список черновиков, ожидающих ревью владельца |
| | `confirm_candidate_memory` | Повысить кандидата до `confirmed` через источник подтверждения пользователя |
| | `reject_candidate_memory` | Предпросмотр удаления кандидата (через confirm-gate) |
| Чтение / ранжирование | `list_personal_memories` | Инвентарь для владельца («что ты запомнил?»), локализация EN/RU |
| | `query_claims` / `query_sources` | Авторизованные метаданные-запросы; закрытые словари фильтров применяются до расшифровки |
| | `get_authorized_claim` / `get_authorized_source` | Чтение одной записи; отсутствующие, чужие и удалённые id дают одинаковый `NotFoundError` |
| | `rank_claims` / `rank_claims_fts` | Детерминированное лексико-сущностное ранжирование с учётом свежести; FTS-вариант использует тенант-скопнутый FTS5-индекс (откат при отсутствии) |
| | `build_claim_fts_index` / `fts_index_present` | Управление опциональным производным FTS5-индексом (хранит открытый текст — см. threat model) |
| | `score_locality` / `bm25_scores` / `apply_snippet_acl` | Tenant-local BM25-хелперы и ACL сниппетов, используемые bench'ем |
| | `assemble_context` | Ограниченный бюджетами `personal` + `session` контекстный бандл для промпта |
| Объяснение / экспорт | `explain_claim` / `explain_why_remembered` | Происхождение, состояние жизненного цикла и опорные источники утверждения |
| | `export_owner_data` / `export_personal_memory` | Полный экспорт владельца (UTF-8 JSON bytes / локализованная сводка) |
| Мутации | `correct_personal_memory` | Добавить исправленную версию утверждения (старая становится `superseded`) |
| | `forget_personal_memory` | Неавторизующий предпросмотр удаления утверждения (через confirm-gate) |
| | `do_not_remember` | Предпросмотр удаления по `claim_id` и/или совпадающему тексту (до 10 записей) |
| Confirm-gate | `SessionBinding` | Хост-провизионированная привязка аутентифицированной сессии, проверяемая гейтом дословно |
| | `create_delete_preview` | Предпросмотр через реестр; nonce не возвращается |
| | `CONFIRM_REGISTRY` (`DeleteRegistry`) | `open_preview(request_id, binding, send=)` доставляет nonce; `consume_confirm(nonce, binding)` тратит его один раз; `reset_registry` для тестов |
| | `consume_delete_request` | Выполняет привязанное удаление в одной транзакции после перепроверки каждой цели |
| | `reject_legacy_confirm` | Отклоняет флаги confirm, поданные моделью |
| Схемы / движки | `TOOL_SCHEMAS` + по-туловые константы `*_SCHEMA` | Канонические схемы инструментов — поверхность MCP `tools/list`; ни одна не принимает селекторов идентичности |
| | `NativeMemoryEngine`, `Mem0Adapter`, `persist_shadow_candidates`, `purge_shadow_projection` | Протокол движков, нативная реализация и чисто протокольный Mem0-shadow слой (без вызовов mem0) |
| Ошибки / типы | `NotFoundError`, `ConflictError`, `ValidationError`, `TrustedDeleteDenied`, `SchemaVersionError`, `MemoryState`, `ErasureReceipt`, `DeleteRequest`, `ConfirmNonce`, `BoundTarget`, `DeleteRegistry` | Возбуждаются/возвращаются перечисленными потоками |

## MCP-сервер (опционально)

```bash
pip install "agent-personal-memory[mcp]"

agent-personal-memory-mcp \
    --actor-platform cli \
    --actor-user-id <user-id> \
    --session-key <key> \
    --session-id <id> \
    --chat-id <chat> \
    [--actor-display-name <name>] [--thread-id <t>] [--profile <p>] \
    [--data-dir <dir>]
```

`python -m agent_personal_memory.mcp` принимает те же флаги. `tools/list`
зеркалит `TOOL_SCHEMAS` дословно; `tools/call` диспетчит в потоки
владельца выше.

**Фиксированная идентичность.** Пять флагов привязки провизионируют `Actor`
и операторский `SessionBinding` один раз при старте; пропущенная или
пустышечная привязка падает сразу (код 2). Аргументы инструментов не могут
нести селекторы актора/тенанта/сессии — лишние поля отклоняются.

**Операторский outbox (confirm-gate через MCP).** Деструктивный вызов
(`forget_personal_memory`, `do_not_remember`, `reject_candidate_memory`)
возвращает только неавторизующий предпросмотр. Фасад затем доставляет
nonce-несущий текст предпросмотра в операторский outbox
`<data_dir>/confirmations.jsonl` — по одной JSON-строке
`{"ts", "request_id", "body"}` — плюс уведомление без nonce в stderr
(stdout несёт только JSON-RPC). Одноразовый nonce никогда не попадает в
результат инструмента. Оператор читает outbox, находит `/memory_confirm
<nonce>` внутри `body` и вызывает инструмент `personal_memory_confirm`,
единственный аргумент которого — `nonce`. Игнорирование предпросмотра —
путь отмены: nonce истекает сам по TTL (600 с).

Замечание о перенесённых текстах: строки предпросмотра всё ещё упоминают
`/memory_delete <request_id>` и `/memory_cancel <request_id>` — команды
хоста-источника, которых нет на этой MCP-поверхности. Строка outbox уже
*является* доставленным предпросмотром; соответствие задокументировано в
SKILL.md.

## Чем отличается

Проверяемые различия в архитектуре и контроле над данными — не бенчмарки.
Факты о других проектах отражают их публичные docs/репозитории по
состоянию на обзор октября 2026; область меняется быстро.

| | agent-personal-memory | Mem0 (`mem0ai`) | Letta | basic-memory |
| --- | --- | --- | --- | --- |
| Упаковка | Встраиваемая Python-библиотека + опциональный локальный stdio MCP + Agent Skill | Python-библиотека + опциональный Docker-сервер + хостинг-платформа; официальный MCP-эндпоинт — облачный | Автономный stateful-рантайм агентов (текущая реализация — TypeScript), не библиотека памяти | Локальный MCP-сервер поверх Markdown-файлов + SQLite-индекс |
| Записи на диске | AES-256-GCM, per-record DEK, обёрнутые файловым KEK | Без шифрования на диске | Открытый Markdown (MemFS) в git-репозитории | Открытый Markdown на диске |
| В цикле | Детерминированные правила + SQLite FTS5/BM25; без LLM и эмбеддингов | LLM + эмбеддер + векторное/графовое хранилище нужны для извлечения и поиска | Память под управлением рантайма | Локальные эмбеддинги (FastEmbed) |
| Деструктивные операции | Двухфазный операторский confirm-gate; nonce не доходит до модели | Прямой вызов инструмента удаления | Инструменты рантайма | Прямой вызов инструмента удаления |
| Тенант-модель | Явный `Actor` на каждый вызов; схемы инструментов не несут селекторов идентичности | user id передаются в аргументах API/инструментов | Рантайм одного агента | На основе workspace |
| Телеметрия | Нет — пакет не делает сетевых вызовов | Opt-out телеметрия включена по умолчанию | — | — |
| Лицензия | MIT | Apache-2.0 | Apache-2.0 | AGPL-3.0 |

Честные компромиссы: пакет **не** делает семантический/векторный поиск и
LLM-извлечение (ранжирование — лексическое/BM25, извлечение — правил-based
EN/RU), у него нет графовой памяти и управляемого облачного уровня.
Выбирайте его, когда приоритет — маленькая аудируемая поверхность
зависимостей (одна runtime-зависимость), шифротекст на диске и удаление с
подтверждением человеком — а не широта recall.

## Заметки по безопасности

- Шифрование только на диске; расшифрованные значения живут в памяти
  процесса на время использования. `master.key` — открытый KEK-файл: кто
  получил его и базу, может расшифровать всё, поэтому защищайте
  `<data_dir>` правами ОС/шифрованием диска и никогда не синхронизируйте и
  не коммитьте его.
- Удаление физическое внутри живой базы, но не дотягивается до бэкапов,
  снапшотов и копий на уровне ОС, сделанных раньше (`ErasureReceipt` несёт
  явную пометку о ретенции бэкапов).
- Опциональный FTS-индекс (`build_claim_fts_index`) хранит открытый текст
  утверждений внутри БД для ранжирования; не стройте его, если на диске
  нужен строго шифротекст — `rank_claims`/`rank_claims_fts` работают без
  него.
- Цепочка поставки: единственная runtime-зависимость `cryptography>=42`;
  SDK `mcp` идёт только в экстре `mcp`. Тесты и bench не требуют сети.
- Подробности: [docs/threat-model.md](docs/threat-model.md).

## Провенанс и лицензия

Ядро извлечено из пакета `plugins/personal_memory` хост-проекта по снапшоту
`f0f6908` в рамках границы извлечения, зафиксированной в
[docs/adr/0002-core-boundary.md](docs/adr/0002-core-boundary.md). Каждый
перенесённый файл, его исходный путь и каждое отклонение отражены в
[docs/PROVENANCE.md](docs/PROVENANCE.md).

Лицензия: [MIT](LICENSE) — Copyright (c) 2026 samohod4ik.

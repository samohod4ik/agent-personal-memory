# agent-personal-memory

Локальная зашифрованная персональная память для ИИ-агентов — библиотека на
Python, опциональный MCP-сервер (stdio) и Agent Skill. Лицензия MIT.

Канонический README на английском: [README.md](README.md)

## Что это

`agent_personal_memory` — хранилище памяти одного владельца, которое хост
агента встраивает напрямую. Данные живут в одном каталоге (`<data_dir>`):
база SQLite (`personal_memory.db`, WAL + `secure_delete`) и файл ключа
шифрования ключей (KEK) на 256 бит (`master.key`, создаётся с правами
0600). Ни сервера, ни сетевых вызовов, ни телеметрии.

- **Шифрование на диске.** Каждое сохранённое поле (содержимое утверждения,
  объяснение, снапшот/локатор/метаданные источника) зашифровано AES-256-GCM
  свежим случайным DEK на запись, обёрнутым файловым KEK. Шифротекст
  привязан через AAD к tenant / entity / version / field, поэтому перенос
  блоба между записями ломает аутентифицированную расшифровку.
  `external_ref` источников хранятся как HMAC-хэши с ключом.
- **Явная идентичность.** Каждый вызов принимает `Actor(platform, user_id,
  display_name)`, построенный хостом; ничего не читается из окружения
  сессии. Межтенантные чтения fail-closed: отсутствующие, чужие и удалённые
  записи возвращают одинаковый `NotFoundError`.
- **Деструктивные операции через подтверждение.** Забывание двухфазное:
  вызов со стороны модели создаёт только неавторизующий предпросмотр;
  одноразовый nonce генерируется внутри гейта подтверждения (confirm-gate),
  доставляется по инжектированному
  операторскому каналу и привязан к точному `SessionBinding` с TTL.
  Переданный моделью флаг `confirm` отклоняется.
- **Физическое удаление.** Подтверждённое удаление уничтожает обёрнутые DEK
  и шифрованные блобы, убирает рёбра поддержки и строки FTS, уплотняет базу
  и оставляет только обезличенную tombstone-квитанцию.
- **Детерминированный recall.** SQLite FTS5 (ограничение по владельцу
  внутри FTS-запроса) плюс лексико-сущностное ранжирование с учётом
  свежести и стемминга RU/EN, и tenant-local BM25 — без эмбеддингов и
  без LLM в цикле.
- **Конфликты решает владелец.** Когда новая запись противоречит
  подтверждённой, детерминированный судья предлагает связь `supersedes`
  вместо тихой перезаписи; только принятие владельцем повышает новую
  запись и вытесняет старую. Полная история по версиям (`reason`,
  принятые связи в обе стороны) и состояние `retracted` для фактов,
  которые перестали быть истиной, но запись о них должна сохраниться.
- **Объяснимость и экспорт.** `explain_claim` / `explain_why_remembered`
  показывают происхождение, состояние жизненного цикла и опорные источники;
  `export_owner_data` делает полный экспорт владельца. Тексты для
  владельца — двуязычные (EN/RU).

См. [docs/threat-model.md](docs/threat-model.md) — что защищается, а что
нет, и [skills/agent-personal-memory/SKILL.md](skills/agent-personal-memory/SKILL.md)
— гайд по встраиванию для агентов.

## Установка

Требуется Python 3.10+. Runtime-зависимости: `cryptography>=42` и
`snowballstemmer>=3.0.1,<4` (BSD-3-Clause).

```bash
pip install .            # из checkout
pip install ".[mcp]"     # с опциональным MCP-сервером
# или напрямую из репозитория при наличии доступа:
pip install "git+ssh://git@github.com/samohod4ik/agent-personal-memory.git"
```

### Обновление с 0.1.x

Миграция схемы v4→v5 выполняется автоматически при первом открытии и
идемпотентна — но базу, переведённую на v5, **нельзя** открыть сборкой
0.1.x. Сделайте резервную копию `<data_dir>` перед обновлением. Чтобы
получить FTS-ранжирование со стеммингом, один раз на тенанта перестройте
производный индекс вызовом `build_claim_fts_index` — до этого
`rank_claims_fts` автоматически откатывается на обычное ранжирование.

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
    # send(text) -> bool. Nonce генерируется внутри confirm-gate, существует
    # только внутри payload отправки и регистрируется лишь после успешного send().
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

## Нормализация поиска

Recall детерминирован и двуязычен. Текст запроса и сохранённый текст
нормализуются одинаково — casefold, `ё`→`е`, стемминг Snowball для
русского и английского (через обязательную зависимость
`snowballstemmer`) — поэтому «какая видеокарта у меня» находит и факт,
записанный как «видеокартой». Токен засчитывается, если он входит
подстрокой (поведение 0.1.x) **или** его основа совпадает с основой из
текста, так что все прежние находки по содержательным словам
сохраняются; ложные срабатывания только по стоп-словам убраны, а запрос
целиком из стоп-слов ведёт себя как раньше. Идентификаторы не стеммятся:
токены с цифрами, смешанная кириллица+латиница, UUID, даты и токены до
6 символов, изначально целиком в верхнем регистре — «RTX 3060»
по-прежнему совпадает точно.

Известные пределы: разные основы Snowball не объединяются
(«созваниваться» не совпадает с «созвоны» — это закреплено
expected-failure тестом), а поверхности, ограниченные `query_claims`,
держат потолок в 500 строк. Опциональный FTS-индекс — v2:
`memory_claim_fts_index` хранит `body` и `body_stem` (открытая копия из
основ — см. threat model) и версию нормализатора в `memory_fts_meta`;
индекс синхронизируется внутри транзакции записи при
create/confirm/correct/accept/retract/erase, restricted- и неактивные
записи в него не попадают, а таблица старого формата или с другой
версией считается отсутствующей (откат на обычное ранжирование), пока
`build_claim_fts_index` не перестроит её для тенанта.

## Согласование замен

Когда новая запись противоречит подтверждённой, библиотека не выбирает
победителя — это делает владелец. Результат `remember_personal_insight`
и сохранённые элементы `learn_from_turn` несут ключ `reconciliations`:
ожидающие решения связи `supersedes` (новая → старая), предложенные
`ConflictJudge` после сравнения не более чем с пятью подтверждёнными
записями того же владельца. Судья по умолчанию `SlotRuleJudge`
(`slot_rule.v1`) детерминирован и консервативен — он сравнивает
несколько личных слотов (дом, имя, обращение; RU и EN) и лучше пропустит
противоречие, чем предложит ложную замену; хост может подставить любого
судью (например, LLM) через `judge=`. Противоречие
оставляет новую запись `candidate`, а старую — `confirmed`;
`accept_memory_reconciliation` повышает новую и вытесняет старую —
предварительно проверив, что старая запись не менялась с момента
предложения, — а `reject_memory_reconciliation` отклоняет замену.

```python
import tempfile

from agent_personal_memory import (
    Actor,
    PersonalMemoryStore,
    accept_memory_reconciliation,
    list_memory_reconciliations,
    personal_memory_history,
    rank_claims,
    remember_personal_insight,
)

with tempfile.TemporaryDirectory(prefix="pm-reconcile-") as data_dir:
    store = PersonalMemoryStore(data_dir=data_dir)
    owner = Actor(platform="cli", user_id="owner-1", display_name="Owner")

    first = remember_personal_insight(owner, "I live in Kazan", store=store)
    assert first["state"] == "confirmed"
    assert first["reconciliations"] == []

    second = remember_personal_insight(
        owner, "I moved to Perm, I no longer live in Kazan", store=store
    )
    # Детерминированный судья видит противоречие: новая запись
    # остаётся кандидатом, а вместо перезаписи создаётся ожидающая
    # решения связь supersedes (новая -> старая).
    assert second["state"] == "candidate"
    link = second["reconciliations"][0]
    assert link["relation"] == "supersedes"
    assert link["status"] == "pending"

    pending = list_memory_reconciliations(owner, store=store)
    assert pending["count"] == 1

    # Решение принимает только владелец: accept повышает новую запись
    # и вытесняет старую.
    resolved = accept_memory_reconciliation(
        owner, link["link_id"], store=store
    )
    assert resolved["status"] == "accepted"
    assert resolved["new_state"] == "confirmed"
    assert resolved["old_state"] == "superseded"

    hits = rank_claims(owner, "where do I live", store=store)
    assert str(hits[0]["claim_id"]) == str(second["claim_id"])

    # История проходит по цепочке версий и принятой связи.
    history = personal_memory_history(
        owner, claim_id=str(first["claim_id"]), store=store
    )
    assert history["current_claim_id"] == str(second["claim_id"])
    assert {entry["claim_id"] for entry in history["entries"]} == {
        str(first["claim_id"]),
        str(second["claim_id"]),
    }
    print(f"current memory: {history['current_claim_id']}")
```

## История и отзыв

`personal_memory_history` (по `claim_id` или `query`) проходит цепочку
версий записи и все принятые замены вокруг неё — хронологические записи
с состоянием, `reason` (`created` / `promoted` / `changed` /
`retracted`), временными метками и содержимым для владельца. Шаг
повышения по умолчанию свёрнут в запись создания (`collapse_promoted`);
`current_claim_id` указывает на новейшую запись цепочки. У версий,
записанных базами 0.1.x, причина выводится из журнала аудита
(`inferred: true`). `explain_claim` несёт те же записи только как
метаданные. Когда факт перестал быть истиной, но запись должна
сохраниться, `retract_personal_memory` добавляет терминальную версию
`retracted`: запись уходит из всех чтений по умолчанию, остаётся
доступной по id и в истории, ожидающие связи с её участием
автоматически отклоняются, а содержимое остаётся зашифрованным, пока
`forget_personal_memory` его не сотрёт.

## Карта API

Все имена ниже импортируются из `agent_personal_memory` (каноническая
поверхность — `src/agent_personal_memory/__init__.py`). Мутирующие и
confirm-gate операции требуют `store` как обязательный именованный
аргумент; у функций чтения `store` по-прежнему по умолчанию `None`.

| Область | Имя | Что делает |
| --- | --- | --- |
| Хранилище / идентичность | `PersonalMemoryStore(data_dir=None)` | Открывает или создаёт каталог хранилища, применяет миграции схемы, загружает или создаёт `master.key` |
| | `Actor(platform, user_id, display_name="")` | Неизменяемая тенант-идентичность; её строит хост, модель её не подаёт |
| | `create_candidate(store, actor, ...)` | Несвязанный метод `PersonalMemoryStore.create_candidate`; всегда создаёт черновик (candidate) |
| Запоминание / обучение | `remember_personal_insight` | Сохранить явный долговечный факт или предпочтение; запись сохраняется как `confirmed` или `candidate` по режиму памяти владельца |
| | `learn_from_turn` | Извлечение долговечных инсайтов из реплики владельца на основе правил (сохраняет только в режиме `auto_insight`) |
| | `get_memory_mode` / `set_memory_mode` | Режим памяти владельца: `auto_insight` (по умолчанию) или `confirm_required` |
| Ревью | `list_candidate_memories` | Список черновиков, ожидающих ревью владельца |
| | `confirm_candidate_memory` | Повысить кандидата до `confirmed` через источник подтверждения пользователя |
| | `reject_candidate_memory` | Предпросмотр удаления кандидата (через confirm-gate) |
| Согласование | `list_memory_reconciliations` | Список связей замены (по умолчанию `pending`) с локализованным заголовком `heading` и видимыми текстами старой и новой записи |
| | `accept_memory_reconciliation` | Принять ожидающую связь `supersedes`: повышает новую запись, вытесняет старую (с проверкой свежести) |
| | `reject_memory_reconciliation` | Пометить ожидающую связь отклонённой; обе записи не меняются |
| | `propose_reconciliation` / `propose_reconciliations` | Ручное предложение связи / проход судьёй; `ConflictJudge`, `SlotRuleJudge`, `Relation` образуют шов судьи (kwarg `judge=`) |
| История / жизненный цикл | `personal_memory_history` | Хронологическая история версий и связей одной записи (`claim_id` или `query`; `collapse_promoted` включён по умолчанию) |
| | `retract_personal_memory` | Терминальное состояние `retracted`: скрыто из чтений по умолчанию, читается по id, содержимое остаётся зашифрованным до forget |
| Чтение / ранжирование | `list_personal_memories` | Инвентарь для владельца («что ты запомнил?»), локализация EN/RU |
| | `query_claims` / `query_sources` | Авторизованные запросы метаданных; фильтры по закрытому словарю применяются до расшифровки |
| | `get_authorized_claim` / `get_authorized_source` | Чтение одной записи; отсутствующие, чужие и удалённые id дают одинаковый `NotFoundError` |
| | `rank_claims` / `rank_claims_fts` | Детерминированное лексико-сущностное ранжирование с учётом свежести; FTS-вариант использует FTS5-индекс с привязкой к тенанту (откат при отсутствии) |
| | `build_claim_fts_index` / `fts_index_present` | Управление опциональным производным FTS5-индексом (хранит открытый текст — см. threat model) |
| | `score_locality` / `bm25_scores` / `apply_snippet_acl` | Tenant-local BM25-хелперы и ACL сниппетов, используемые набором bench |
| | `assemble_context` | Контекстный бандл `personal` + `session` для промпта с ограничением бюджетов |
| Объяснение / экспорт | `explain_claim` / `explain_why_remembered` | Происхождение, состояние жизненного цикла и опорные источники утверждения |
| | `export_owner_data` / `export_personal_memory` | Полный экспорт владельца (UTF-8 JSON bytes / локализованная сводка) |
| Мутации | `correct_personal_memory` | Добавить исправленную версию утверждения (старая становится `superseded`) |
| | `forget_personal_memory` | Неавторизующий предпросмотр удаления утверждения (через confirm-gate) |
| | `do_not_remember` | Предпросмотр удаления по `claim_id` и/или совпадающему тексту (до 10 утверждений) |
| Confirm-gate | `SessionBinding` | Привязка аутентифицированной сессии, провизионируемая хостом и проверяемая confirm-gate дословно |
| | `create_delete_preview` | Предпросмотр через реестр; nonce не возвращается |
| | `CONFIRM_REGISTRY` (`DeleteRegistry`) | `open_preview(request_id, binding, send=)` доставляет nonce; `consume_confirm(nonce, binding)` расходует его однократно; `reset_registry` для тестов |
| | `consume_delete_request` | Выполняет привязанное удаление в одной транзакции после перепроверки каждой цели |
| | `reject_legacy_confirm` | Отклоняет флаги confirm, поданные моделью |
| Схемы / движки | `TOOL_SCHEMAS` + константы `*_SCHEMA` для каждого инструмента | Канонические схемы инструментов — поверхность MCP `tools/list`; ни одна не принимает селекторов идентичности |
| | `NativeMemoryEngine`, `Mem0Adapter`, `persist_shadow_candidates`, `purge_shadow_projection` | Протокол движков, нативная реализация и чисто протокольный Mem0-shadow слой (без вызовов mem0) |
| Ошибки / типы | `NotFoundError`, `ConflictError`, `ValidationError`, `TrustedDeleteDenied`, `SchemaVersionError`, `MemoryState`, `ErasureReceipt`, `DeleteRequest`, `ConfirmNonce`, `BoundTarget`, `DeleteRegistry` | Возбуждаются/возвращаются перечисленными выше операциями |

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
зеркалит `TOOL_SCHEMAS` дословно; `tools/call` диспетчит в перечисленные
выше операции владельца.

**Фиксированная идентичность.** Пять флагов привязки провизионируют `Actor`
и операторский `SessionBinding` один раз при старте; отсутствующая или
заглушечная привязка — немедленный отказ при запуске (код 2). Аргументы
инструментов не могут
нести селекторы актора/тенанта/сессии — лишние поля отклоняются.

**Операторский outbox (confirm-gate через MCP).** Деструктивный вызов
(`forget_personal_memory`, `do_not_remember`, `reject_candidate_memory`)
возвращает только неавторизующий предпросмотр. Фасад затем доставляет
текст предпросмотра, содержащий nonce, в операторский outbox
`<data_dir>/confirmations.jsonl` — по одной JSON-строке
`{"ts", "request_id", "body"}` — плюс уведомление без nonce в stderr
(stdout несёт только JSON-RPC). Одноразовый nonce никогда не попадает в
результат инструмента. Оператор читает outbox, находит `/memory_confirm
<nonce>` внутри `body` и вызывает инструмент `personal_memory_confirm`,
единственный аргумент которого — `nonce`. Игнорирование предпросмотра —
путь отмены: nonce истекает сам по TTL (600 с). Строки, чей TTL nonce уже
прошёл, вычищаются при следующей записи: outbox — временный канал
доставки, а не журнал аудита.

Замечание о перенесённых текстах: строки предпросмотра всё ещё упоминают
`/memory_delete <request_id>` и `/memory_cancel <request_id>` — команды
хоста-источника, которых нет на этой MCP-поверхности. Строка outbox уже
*является* доставленным предпросмотром; соответствие задокументировано в
SKILL.md.

## Чем отличается

Проверяемые различия в архитектуре и контроле над данными — не бенчмарки.
Факты о других проектах отражают их публичные документы и репозитории по
состоянию на момент обзора (октябрь 2026); область меняется быстро.

| | agent-personal-memory | Mem0 (`mem0ai`) | Letta | basic-memory |
| --- | --- | --- | --- | --- |
| Упаковка | Встраиваемая Python-библиотека + опциональный локальный stdio MCP + Agent Skill | Python-библиотека + опциональный Docker-сервер + хостинг-платформа; официальный MCP-эндпоинт — облачный | Автономный рантайм агентов с состоянием (текущая реализация — TypeScript), не библиотека памяти | Локальный MCP-сервер поверх Markdown-файлов + SQLite-индекс |
| Записи на диске | AES-256-GCM, DEK на запись, обёрнутые файловым KEK | Без шифрования на диске | Открытый Markdown (MemFS) в git-репозитории | Открытый Markdown на диске |
| В цикле | Детерминированные правила + SQLite FTS5/BM25; без LLM и эмбеддингов | LLM + эмбеддер + векторное/графовое хранилище нужны для извлечения и поиска | Память под управлением рантайма | Локальные эмбеддинги (FastEmbed) |
| Деструктивные операции | Двухфазный операторский confirm-gate; nonce не доходит до модели | Прямой вызов инструмента удаления | Инструменты рантайма | Прямой вызов инструмента удаления |
| Тенант-модель | Явный `Actor` на каждый вызов; схемы инструментов не несут селекторов идентичности | user id передаются в аргументах API/инструментов | Рантайм одного агента | На основе workspace |
| Телеметрия | Нет — пакет не делает сетевых вызовов | Opt-out телеметрия включена по умолчанию | — | — |
| Лицензия | MIT | Apache-2.0 | Apache-2.0 | AGPL-3.0 |

Честные компромиссы: пакет **не** делает семантический/векторный поиск и
LLM-извлечение (ранжирование — лексическое/BM25, извлечение — на основе
правил, EN/RU), у него нет графовой памяти и управляемого облачного уровня.
Выбирайте его, когда приоритет — маленькая аудируемая поверхность
зависимостей (две runtime-зависимости), шифротекст на диске и удаление,
подтверждаемое человеком — а не широта recall.

## Заметки по безопасности

- Шифрование только на диске; расшифрованные значения живут в памяти
  процесса на время использования. `master.key` — открытый KEK-файл: кто
  получил его и базу, может расшифровать всё, поэтому защищайте
  `<data_dir>` правами ОС/шифрованием диска и никогда не синхронизируйте и
  не коммитьте его.
- Мутирующие операции владельца (`remember_personal_insight`,
  `learn_from_turn`, `correct_personal_memory`, `forget_personal_memory`,
  `do_not_remember`, `confirm_candidate_memory`, `reject_candidate_memory`)
  требуют `store` как обязательный именованный аргумент — запись никогда не
  уходит в неявное дефолтное хранилище. Хелперы согласования и истории
  (`list_memory_reconciliations`, `accept_memory_reconciliation`,
  `reject_memory_reconciliation`, `personal_memory_history`,
  `retract_personal_memory`) требуют `store=` так же. Функции чтения
  по-прежнему принимают `store=None`, который открывает, а при
  отсутствии создаёт, каталог данных по умолчанию
  (`PERSONAL_MEMORY_DATA_DIR`, иначе `~/.agent-personal-memory`); при
  встраивании передавайте явный `store=` везде, чтобы забытый аргумент
  не читал дефолтное расположение хоста вместо нужного хранилища.
- Удаление физическое внутри живой базы, но не распространяется на бэкапы,
  снапшоты и копии на уровне ОС, сделанные раньше (`ErasureReceipt` несёт
  явную пометку о ретенции бэкапов).
- Опциональный FTS-индекс (`build_claim_fts_index`) хранит открытый текст
  утверждений внутри БД для ранжирования — `body` и вторую открытую
  производную копию `body_stem`; не стройте его, если на диске нужен
  строго шифротекст — `rank_claims`/`rank_claims_fts` работают без него.
- Цепочка поставок: runtime-зависимости `cryptography>=42` и
  `snowballstemmer>=3.0.1,<4`; SDK `mcp` поставляется только в
  дополнительной зависимости (extra) `mcp`. Тесты и bench не требуют сети.
- Подробности: [docs/threat-model.md](docs/threat-model.md).

## Провенанс и лицензия

Ядро извлечено из пакета `plugins/personal_memory` хост-проекта по снапшоту
`f0f6908` в рамках границы извлечения, зафиксированной в
[docs/adr/0002-core-boundary.md](docs/adr/0002-core-boundary.md). Каждый
перенесённый файл, его исходный путь и каждое отклонение отражены в
[docs/PROVENANCE.md](docs/PROVENANCE.md).

Лицензия: [MIT](LICENSE) — Copyright (c) 2026 samohod4ik.

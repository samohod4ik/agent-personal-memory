# Слоистая память — план реализации, revision 1

## Цель и граница поставки

Реализовать [#6](https://github.com/samohod4ik/agent-personal-memory/issues/6)
и подготовить связанный PR для [Hermes #249](https://github.com/samohod4ik/Hermes/issues/249).
Результат этой работы — проверенный код и два PR. Установка в Windows production,
живые модельные прогоны и закрытие интеграционного issue требуют отдельной фактической приёмки.
Пользователь поручил составить, перепроверить и исполнить план без промежуточной передачи.
Исходные версии: библиотека db357f4 (v0.2.0), Hermes 5618603e (schema v4).

Архитектурный референс: Iva, agent-second-brain и autograph Сергея Шимы
(Serge Shima, smixs). Собственная реализация концепций; исходники Iva не копируются.
Одна библиотека, существующее зашифрованное хранилище, тонкий адаптер Hermes.
Существующие GitHub issues остаются источником требований; этот файл — единый план.
Не добавляем отдельный workflow engine, графовую БД, обязательную модель или scheduler.

## Требования и наблюдаемая приёмка

| ID | Требование | Проверка |
| --- | --- | --- |
| R1 | Журнал оригиналов с устойчивым ID, ролью, временем, версиями и evidence | Повтор не дублируется; одинаковый текст разных событий различается; metadata/edit обновляет версию; источник не подменяется insight |
| R2 | Core и recall используют актуальные разрешённые данные и единый бюджет | Новая сессия, >500 событий, неизвестный факт, кандидат/restricted/другой Actor, общий token budget |
| R3 | Day/week/month/year, ссылки, экспорт | Полуоткрытые периоды в timezone, отсутствие двойного счёта, сегодняшний recall, Markdown с источниками |
| R4 | Ограниченная инкрементальная консолидация | Повтор неизменного ввода без вызова; ошибка/сбой/late edit; устаревший результат не применяется; лимиты и usage |
| R5 | Коррекция и forget без восстановления забытых данных | Актуальный core/summary/graph; удаление целевых spans и производных; общий источник сохраняет другие факты; tombstone блокирует replay |
| R6 | Совместимость Hermes и приватность | Миграция синтетической v4-копии, один writer, сохранение Travel/S02, opt-in raw capture, authenticated private chat, no-remember |
| R7 | Проверяемая интеграция и авторство | Новый memory_layered в tgfake; component tests отдельно от Windows gateway/LLM; README/PROVENANCE/инструкция и ссылки на автора |

## Архитектура и интерфейсы

Расширить схему библиотеки аддитивно до v6: индекс событий, evidence по версии
claim/source и диапазону текста, зависимости проекций, контроль выполнения jobs.
Тексты/структурированные payload хранятся существующими encrypted records;
опциональный старый FTS остаётся plaintext — документация явно говорит об этом.

Host-only API: `record_turn`, `query_journal`, `remember_from_evidence`,
`build_core`, `consolidate_period`, `recall`, `export_markdown`.
Везде явные Actor/store. Захват имеет явную политику и срок хранения;
role/origin/identity поступают от host, не из аргументов инструмента модели.
Core строится из актуальных confirmed/nonrestricted claims; не копируется
отдельным постоянно устаревающим текстом. Источники — данные, не инструкции.

Сводка имеет granularity, timezone, точные границы, coverage и fingerprint
входных версий. Экстрактивный режим работает без сети. Внедряемый adapter
возвращает структурированные предложения и usage; вызов вне DB-транзакции,
проверка свежести перед commit. Свободное предложение модели само по себе
не становится подтверждённым фактом. Неизвестная usage не считается нулевой.
Ссылки имеют тип и проверенный источник; фильтрация доступа предшествует обходу.
Удаление работает и для проекций/checkpoints, сохраняет unrelated spans;
старые внешние backups не объявляются мгновенно стёртыми.

## Исполнение

1. **L — библиотека, один implementer.** Создать journal/layered/consolidation,
   изменить store migration/erasure, learn source binding и assembler budget.
   Добавить API exports, целевые tests и небольшой фиксированный bench.
   Обновить README, CHANGELOG, PROVENANCE и описание API/миграции.
   Проверка: `python -m unittest discover -s tests`, `python -m unittest discover -s bench`,
   `python tools/hygiene_scan.py`; ожидается отсутствие ошибок, все новые AC выполнены.
2. **H — Hermes, отдельный implementer.** После фиксации API подключить библиотеку
   через pinned dependency и совместимый host store wrapper; не оставлять
   два несовместимых writer. Сохранить host identity, Travel assembler и S02.
   Подключить opt-in journal к authenticated ingress и context к inject;
   добавить version-bound runtime patch для полного generated assistant response (отдельно от доставки): применение patch проверить offline, Windows NOT RUN;
   добавить конечный memory tick по существующему образцу weekly-review-tick,
   вызываемый штатным Windows Task Scheduler; не вмешиваться в routines worker/M6.
   Добавить memory_layered через реальный dispatcher/plugin, component tests,
   миграционный тест на v4-copy и инструкцию включения/возврата.
   Проверка: адресные memory tests и сценарий harness; Windows gateway результат
   отмечается NOT RUN, если этот runtime недоступен в облачной среде.
3. **I — интеграция, root.** Установить локальную библиотеку в тестовое окружение
   Hermes, сверить совместимость API, выполнить адресные regression tests.
   Независимый read-only reviewer проверяет план до кода, затем совокупный diff
   и результаты (bug/security/API/docs). Исправлять подтверждённые дефекты;
   повторять только затронутые проверки. Не запускать общую приёмку #166–170.
4. **PR.** Зафиксировать library commit, pin Hermes на него, открыть два связанных PR
   с фактическими командами/результатами и ограничениями. Не merge/deploy.

## Восстановление и ограничения

До upgrade существующего store — согласованная SQLite backup-копия и прежний ключ;
возврат старого runtime возможен только с прежней копией DB, не поверх v6.
Миграционный тест работает на синтетике. Production, Gmail, Finance, M6 и ключи
пользователя не изменяются. Никаких новых облачных расходов.
Реальные LLM extraction quality и Windows tgfake не заменяются synthetic PASS.
Роли агентов наследуют модель текущей сессии согласно возможностям harness;
по одному writer на репозиторий. Сохранённые замечания и результаты — в PR,
без отдельной цепочки handoff-файлов.

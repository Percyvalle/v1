# Twitch Anti-Bot Moderation System — план

Статус: **план подтверждён, идёт поэтапная реализация.**

## Прогресс реализации (обновляется по ходу работы)

- ✅ Этап 0 — pyproject.toml, pytest/ruff/mypy strict, requirements-dev.txt
- ✅ Этап 1 — `bot/moderation/{types,normalize,window}.py` + 88 тестов, всё зелёное
- ✅ Этап 2 — детекторы + scoring + confidence: config.py,
  detectors/{base,burst,duplicate,links,unicode,language,account,username,emote}.py,
  scoring.py, confidence.py, config/moderation.yml, config/channels/example.yml.
  188/188 тестов зелёных, ruff чисто, mypy strict без замечаний, main.py бота
  по-прежнему импортируется. По пути исправлено: тест skeleton_match проверял
  сценарий, где minhash-схожесть (0.81) уже проходила порог near_duplicate
  раньше, чем очередь доходила до skeleton — подобран пример с разными
  (не повторяющимися подряд) цифрами одинаковой длины, где skeleton совпадает,
  а схожесть текста ниже порога; вес unexpected_language снижен с 5 до 3,
  чтобы он был строго ниже любого другого сигнала, а не просто равен минимуму.
- ✅ Этап 3 — `bot/moderation/clustering.py` (union-find, рёбра по общему
  домену/minhash/skeleton, стоп-лист коротких фраз, `ClusterConfig` в
  config.py + YAML). `ClusterInfo` дополнен полями `risk_score`/`confidence`
  и методом `to_dict()`. 206/206 тестов зелёных (18 новых в
  test_clustering.py), ruff и mypy strict чисты. Прямые сценарии из ТЗ
  подтверждены: 50 ботов за 3-20 сек образуют один кластер размера 50;
  10 разных польскоязычных сообщений — ни одного кластера. По пути найдено
  и задокументировано архитектурно важное: find_clusters() в одиночку видит
  только TIMING+NETWORK (2 семейства), поэтому confidence упирается в
  потолок family_factor_for(2)=0.70 — ниже порога автотаймаута (0.75) даже
  для плотной атаки из 50 ботов. Это намеренно: полный вердикт с
  подтверждением от CONTENT/IDENTITY/ENCODING собирает только engine.py на
  этапе 5, объединяя кластерные сигналы с сигналами конкретных сообщений.
- ✅ Этап 4 — схема БД, миграции, store.py
- ✅ Этап 5 — подключение к main.py в SHADOW-режиме
- ✅ Этап 6 — replay CLI + прогон на 7271 реальном сообщении из bot.db/bot.ari.db
- ✅ Этап 7 — Helix-клиент, executor, очередь действий
- ✅ Этап 8 — панель модерации (Live + Audit), роли, аудит
- ✅ Twitch OAuth для входа в панель (вне нумерации, добавлено сразу после этапа 8)
- ✅ Этап 9 — Bot Pattern Library, Attack Mode, trusted, FP-статистика (подэтапы 9a-9d)
- ✅ Панельные экраны Patterns/Attack Mode/Stats + кнопка false positive
  (вне нумерации, добавлено сразу после этапа 9 — UI поверх REST из 9b-9d)
- ✅ Токен модератора через панель + executor подключён к main.py + экраны
  Users/Settings (вне нумерации — закрывает блокер #1 раздела 11 целиком)
- ⬜ Этап 10 — включение LIVE-режима (решение владельца канала)

- ✅ Этап 4 — `bot/moderation/migrations.py` (версионированные миграции по
  `PRAGMA user_version`, идемпотентны) + `bot/moderation/store.py`
  (`ModerationStore`: upsert_user, get_user_state, save_message, save_verdict
  + сигналы, save_cluster + участники). Схема ограничена тем, что реально
  нужно этапам 5-6 (mod_users, mod_messages, mod_verdicts, mod_signals,
  mod_clusters, mod_cluster_members) — остальные таблицы из раздела 8 плана
  (mod_actions, mod_action_queue, mod_trusted, mod_panel_users, mod_patterns,
  mod_feedback, mod_stats_daily) добавятся новыми миграциями на этапах 7-9,
  когда появится код, который их использует, а не создаются впрок.
  225/225 тестов зелёных (19 новых: test_migrations.py, test_store.py),
  ruff и mypy strict чисты. Отдельно подтверждено тестом: новые таблицы
  mod_* не конфликтуют с существующими viewers/recent_messages в одном
  файле БД. Попутно поправлено: mypy strict неожиданно затягивал в проверку
  существующий нетипизированный bot/database.py (тесты его импортируют для
  проверки отсутствия коллизий схем) — добавлен точечный override
  `ignore_errors=true` для этого модуля в pyproject.toml, сам файл не тронут.

- ✅ Этап 5 — `bot/moderation/policy.py` (risk_score+confidence → recommended_action,
  инварианты: BAN требует 2+ семейства сигналов, мод/VIP/стример и
  trusted/marked_safe — максимум OBSERVE, оба закрыты кодом, а не конфигом)
  и `bot/moderation/engine.py` (`ModerationEngine.observe(event) -> Verdict` —
  единственная точка входа, оркестрирует нормализацию → детекторы →
  кластеризацию → scoring → confidence → policy → аудит). Подключено к
  `main.py`: новое поле `Config.moderation_enabled` (env `MODERATION_ENABLED`,
  по умолчанию `true`), движок создаётся один раз при старте бота и
  привязывается к `event_message` через `_observe_moderation()` — строит
  `ChatEvent` из объекта twitchio (user_id, badges, is_first_message из тега
  `first-msg`, is_returning_chatter из сырого тега `returning-chatter`) и
  вызывает `engine.observe()` в try/except, чтобы сбой модерации никогда не
  ронял обработку чата ботом. Режим жёстко зашит как SHADOW (пишет вердикты
  в БД и лог при risk выше LOW, ничего не банит) — переключение
  SIMULATION/LIVE и автодействия появятся на этапах 7/9.
  257/257 тестов зелёных (32 новых: test_policy.py — все инварианты
  проверены и в AGGRESSIVE/ATTACK/SAFE режимах отдельно, test_engine.py —
  оба сценария ТЗ прогнаны через ПОЛНЫЙ движок, а не по отдельным слоям),
  ruff и mypy strict чисты. Проверено вручную через мок-объекты twitchio:
  конвейер от «сообщения в чате» до вердикта с логированием работает
  end-to-end и корректно эскалирует OBSERVE→TIMEOUT→BAN по мере накопления
  независимых сигналов. По пути найден и исправлен реальный баг:
  `policy.build_reason()` проверял `not signals` раньше `blocked_by`,
  из-за чего привилегированный пользователь без сигналов получил бы
  объяснение «ничего не найдено» вместо настоящей причины понижения
  действия — порядок проверок переставлен.

- ✅ Этап 6 — `bot/moderation/replay.py` (чтение recent_messages в режиме
  "только чтение", синтез ChatEvent из исторических данных с явно
  задокументированными допущениями — нет user_id/badges/возраста
  аккаунта в старых логах, `run_replay()` прогоняет события через движок
  БЕЗ store, `ReplayReport` со сводкой) + `scripts/replay.py` (тонкий CLI:
  `python scripts/replay.py --db bot.db --channel yca4crew`).

  **Реальный прогон на 8207 сообщениях (bot.db: 5743 + bot.ari.db: 2464) —
  главная проверка стадии: 0 TIMEOUT, 0 BAN на обоих логах.** Критерий
  регрессии из раздела 12 плана выполнен.

  По пути replay нашёл реальный, а не гипотетический баг: `detectors/duplicate.py`
  не имел защиты от коротких фраз (в отличие от `clustering.py`, где она
  была с самого начала) — "ку" (обычное приветствие) от разных зрителей
  давало ложный `exact_duplicate`. Добавлено поле `min_content_length: int = 6`
  в `DuplicateConfig`, симметрично гасящее сравнение с обеих сторон (как в
  `clustering.py._has_edge`). После фикса число ложных `OBSERVE` на bot.db
  упало с 20 до 13.

  Отдельная находка, оставленная задокументированной, а не исправленной
  ceй час: небольшая группа постоянных зрителей (`tema7_5`, `digitalthevoid`,
  `alandalone`, `ted777xc`, `houston_ksenia`) регулярно устраивает
  копипаста-перекличку («вы че повторяете?», «Кто тут попка» — синхронно,
  друг за другом) — структурно похоже на координированную атаку
  (exact_duplicate + synchronized_arrival + cluster_membership), но это
  органичное поведение известных постоянных зрителей, не боты. Система
  корректно ограничивает это уровнем OBSERVE (макс. risk=54 из 100, порог
  TIMEOUT=60 ни разу не пройден) — false positive protection уже работает
  на практике. Полноценная защита "known active participant" (пункт D из
  раздела 10 ТЗ, автоматическое доверие по истории) осталась в scope
  этапа 9 (Trusted Users), как и планировалось — эти реальные данные
  прямо подтверждают, зачем она там нужна.

  270/270 тестов зелёных (18 новых в test_replay.py, плюс 2 новых теста
  на короткие фразы в test_duplicate.py), ruff чист, mypy strict чист
  (bot/moderation + tests/moderation по конфигу, scripts/replay.py
  проверен отдельным ручным прогоном `mypy --strict`, т.к. вне scope
  files в pyproject.toml).

- ✅ Этап 7 — `bot/moderation/twitch_api.py` (независимый от twitchio
  httpx-клиент Helix: App Access Token для `GET /helix/users` — не требует
  scope, резолвит `Verdict.is_provisional`; `ban_user`/`timeout_user`
  через `POST /helix/moderation/bans`; `delete_chat_messages`; ретраи с
  экспоненциальным backoff на 429/5xx, рейт-лимитер, батчинг по 100 для
  get_users) + `bot/moderation/executor.py` (`ActionExecutor` — исполняет
  `ActionRequest` поштучно на каждого пользователя, т.к. Helix ban не
  батчевый, собирает `ExecutionOutcome` вида "14/17 выполнено"; `parse_payload`
  с полной рантайм-валидацией JSON из очереди; `process_pending` — обработка
  пачки заданий) + миграция 002 (`mod_action_queue`, `mod_actions`,
  отложенные с этапа 4 именно сюда) + методы `store.py` для очереди/аудита.

  **Вопрос токена с scope `moderator:manage:banned_users` сознательно НЕ
  решён** — по решению пользователя, весь модуль построен и полностью
  протестирован на моках `httpx.MockTransport`, без единого реального
  запроса к Twitch. `ban_user`/`timeout_user`/`delete_chat_messages` при
  вызове с текущим chat-only токеном вернут 401 — это ожидаемо и не баг;
  реальные действия появятся только после того, как токен будет
  перевыпущен с нужными scope (решение пользователя, не техническое).

  Executor **не подключён к main.py** — сознательное решение по scope:
  подключать очередь, которую сейчас некому наполнять (панель — этап 8),
  означало бы держать в проде бесполезный простаивающий поллер. Модуль
  полностью готов к использованию, когда появится источник заданий.

  По пути найден и исправлен реальный баг в `get_users()`: батчинг по
  100 логинов/id изначально фильтровал через `in` на срезе списка — при
  дублирующихся значениях это могло взять элементы не из того батча.
  Переписано на прямое разбиение единого тегированного списка параметров.

  316/316 тестов зелёных (52 новых: test_twitch_api.py — 15, test_executor.py — 22,
  плюс 15 новых в test_store.py на очередь/аудит), ruff и mypy strict чисты.
  Полный цикл проверен вручную: очередь → executor → мок Helix →
  прогресс → аудит с привязкой к модератору-инициатору и cluster_id.

- ✅ Этап 8 — панель модерации: `panel/moderation_api.py` (роутер FastAPI,
  подключается к существующему `app` в `panel/server.py` через
  `include_router`, + `GET /moderation` отдаёт страницу), `panel/static/
  moderation.html`/`moderation.js` (переиспользуют CSS-токены `index.html`,
  как требовал раздел 7 плана — отдельная страница, не вкладка).

  **По решению пользователя объём сужен до Live + Audit** (полный список из
  7 экранов — Users/Clusters/Patterns/Settings/Stats — отложен: Patterns/
  Settings/Stats всё равно не на чём строить до этапа 9). Live показывает
  активные кластеры (`GET /api/moderation/clusters`, отсортированы по
  риску) с кнопками `BAN ALL`/`TIMEOUT ALL`/`MARK SAFE`/`IGNORE` и модалкой
  подтверждения с точным числом пользователей, плюс ленту подозрительных
  вердиктов. Audit — таблица `mod_actions` (кто/роль/действие/причина/итог).
  Обновление — WebSocket `/api/moderation/ws`, опрашивает БД раз в 2 сек и
  шлёт снапшот {clusters, verdicts}, с автопереподключением на фронте при
  разрыве.

  **Роли** (`OWNER`/`ADMIN`/`MODERATOR`/`VIEWER`) проверяются в роутере, а
  не в JS — `require_role()` перед каждым мутирующим эндпоинтом; попытка
  выдать роль `OWNER` требует самому быть `OWNER` (иначе `ADMIN` мог бы
  себя повысить). Ровно как задокументировано в разделе 7/11 плана: панель
  пока слушает только `127.0.0.1` без аутентификации, поэтому роль и ник
  передаются клиентом явно через заголовки `X-Panel-Role`/`X-Panel-Actor`
  (хранятся в localStorage на фронте) — это модель данных на будущее
  (готовая таблица `mod_panel_users`, миграция 003), а не защита прямо
  сейчас; когда появится Twitch OAuth, поменяется только `current_role()`.

  Кнопка `BAN ALL`/`TIMEOUT ALL` кладёт задание в `mod_action_queue` (та же
  БД профиля) — им наполняет очередь панель, исполняет `executor.py` в
  процессе бота (этап 7). Это тот самый реальный потребитель очереди,
  которого не хватало на этапе 7 для подключения поллинга к `main.py` —
  но само подключение `process_pending()` к циклу бота сознательно
  оставлено вне этого этапа: нужен решённый вопрос токена с scope
  `moderator:manage:banned_users` (раздел 11, блокер #1), иначе любое
  реальное исполнение упадёт на 401 — подключать поллер сейчас означало бы
  включить код, который не может сделать ничего полезного.

  336/336 тестов зелёных (20 новых в `tests/panel/test_moderation_api.py`:
  роли и права на каждом мутирующем эндпоинте, фильтрация кластеров по
  статусу, аудит с деталями, WebSocket-снапшот через `TestClient.
  websocket_connect`), ruff и mypy strict чисты (`panel/moderation_api.py`
  и `tests/panel` добавлены в strict-scope `pyproject.toml`, как постоянный
  код проекта — в отличие от разового `scripts/replay.py`). Вручную
  проверено на реальном `bot.db` через живой uvicorn-процесс (порт 8766,
  чтобы не трогать уже запущенную у пользователя панель на 8765):
  `/moderation` и `/static/moderation.js` отдаются, `GET /api/moderation/
  {clusters,audit,verdicts}` отвечают корректно (пусто — чат тихий),
  `POST /api/moderation/actions` без заголовка роли отдаёт 403.

  По пути найден и исправлен баг в собственном тесте (не в проде): профиль
  без своего `.env.<profile>` резолвится в тот же `bot.db`, что и `main`
  (пустой `INSTANCE` — тот же файл), поэтому `profile=does_not_exist`
  ошибочно ожидался как 404 — переписано на профиль со своим `.env.other`
  (`INSTANCE=ghost`), у которого действительно нет файла `bot.ghost.db`.

- ✅ **Вход в панель через Twitch OAuth** (вне нумерации этапов — закрыт по
  прямому запросу пользователя сразу после этапа 8, увидевшего вживую, что
  выпадающий список роли в UI ничем не защищён). `panel/auth.py` — новый
  модуль: Authorization Code Grant (`GET /auth/login` → редирект на Twitch
  с `state` для CSRF → `GET /auth/callback` → обмен `code` на user-токен →
  роль). Подписанная cookie-сессия через `starlette.middleware.sessions.
  SessionMiddleware` (новая зависимость `itsdangerous`) — `panel/server.py`
  подключает `SessionMiddleware` + `auth.router`, кладёт
  `app.state.panel_auth_config`/`app.state.moderation_store_factory`.
  Роль вычисляется автоматически: сам канал (`login == broadcaster_login`)
  → `OWNER`; модератор канала → `MODERATOR`; иначе → `VIEWER`; ручной
  оверрайд на `ADMIN`/`OWNER` через существующую `mod_panel_users` (миграция
  003, этап 8) применяется поверх статуса из Twitch. `moderation_api.py`
  переведён с заголовков `X-Panel-Role`/`X-Panel-Actor` (клиент сам заявлял
  о себе, без проверки) на `panel.auth.require_authenticated` — теперь
  каждый эндпоинт, включая read-only (`/clusters`, `/verdicts`, `/audit`),
  требует реальную сессию, а не просто заявленную роль (решение пользователя:
  данные о зрителях канала не должны быть видны анонимно в локальной сети).
  Фронтенд (`moderation.html`/`.js`) лишился выпадающих списков роли/ника —
  вместо них экран логина (`GET /auth/me` на старте, редирект на 401),
  карточка пользователя в сайдбаре, кнопка «Выйти».

  **Живой прогон против настоящего Twitch выявил и исправил 2 реальные
  ошибки** (не гипотетические — обе поймана только на живом входе
  модератора, не на моках):
  1. Неверное имя OAuth scope: `moderator:read:moderators` не существует
     для нужной цели — Twitch отвечал `401 Missing scope: moderation:read`.
  2. После исправления на `moderation:read` выяснилось, что сам эндпоинт
     `GET /helix/moderation/moderators` ("кто модераторы канала")
     принципиально требует, чтобы токен принадлежал **broadcaster'у** —
     обычный модератор получает `401 The ID in broadcaster_id must match
     the user ID found in the request's OAuth token`, даже с правильным
     scope. Решение — другой эндпоинт, `GET /helix/moderation/channels`
     ("Get Moderated Channels", scope `user:read:moderated_channels`):
     вопрос переформулирован с "кто модераторы ЭТОГО канала" (нужен токен
     владельца) на "в каких каналах модератор Я" (работает с токеном
     самого модератора). Итоговый flow подтверждён вживую: вход под
     аккаунтом-модератором канала `paverpapa` корректно вернул роль
     `MODERATOR`.

  356/356 тестов зелёных (37 новых: `tests/panel/test_auth.py` — конфиг,
  резолвер ролей, полный OAuth callback на `httpx.MockTransport` для всех
  трёх ролей + admin-оверрайда; `tests/panel/test_moderation_api.py`
  переписан на `login_as()` вместо заголовков), ruff и mypy strict чисты
  (`panel/auth.py` добавлен в strict-scope).

## Этап 9 — план (подтверждён, реализация по подэтапам 9a→9d)

Каждый подэтап — со своим прогоном тестов/ruff/mypy, как и на этапах 1-8, а не
единой правкой. Инфраструктура под все 4 части уже частично заложена раньше
(намеренно, чтобы не потребовалось трогать сигнатуры): `confidence()` уже
принимает `fp_penalty`, `TrustLevel`/`Sensitivity.ATTACK` уже в `types.py`,
`mod_trusted`/`mod_patterns`/`mod_feedback` намечены в разделе 8 схемы.

### ✅ 9a. Trusted Users + автоматическое доверие по истории — ЗАВЕРШЕНО

- Миграция 004: таблица `mod_trusted(user_id PK, added_by, added_at, reason)`.
- `store.py`: `mark_trusted()`, `is_trusted()`, `list_trusted()`.
- `panel/moderation_api.py`: `POST /api/moderation/users/{user_id}/mark_safe`
  (ручной MARK AS SAFE — раздел 10 ТЗ), пишет в `mod_trusted` и обновляет
  `mod_users.marked_safe`/`marked_safe_by`/`marked_safe_at`.
- **Автоматическое доверие по истории** (пункт D раздела 10 ТЗ, обосновано
  находкой replay на этапе 6: `tema7_5`/`digitalthevoid`/`alandalone` и др.
  регулярно устраивают копипаста-перекличку, структурно похожую на атаку,
  но это органичное поведение постоянных зрителей). `engine.py` при загрузке
  `UserState` поднимает `trust_level` с `UNKNOWN` до `REGULAR`, когда
  `message_count` и `first_seen`-давность превышают настраиваемые пороги
  (новые поля `TrustConfig` в `config.py`: `min_messages_for_regular`,
  `min_days_for_regular` — оба должны выполниться, иначе бот-аккаунт,
  проживший месяц ничего не делая, а затем внезапно начавший спамить,
  всё равно не получит доверия просто по возрасту). `REGULAR` — это НЕ
  `TRUSTED`: `UserState.is_protected` по-прежнему требует `TRUSTED`+, но
  `REGULAR` дополнительно снижает `risk_score` через новый слабый
  контекстный множитель в `scoring.py` (не отдельный сигнал — история
  недостаточно специфична, чтобы быть "признаком", это модификатор веса
  уже найденных сигналов, отдельно проверяемый тестом на инвариант "не
  может сам по себе понизить CRITICAL до NOTHING").
- Тесты: `test_store.py` (CRUD trusted), `test_engine.py` (REGULAR не блокирует
  BAN у аккаунта с 2+ семействами при реальной атаке, но снижает risk
  органичной копипаста-переклички ниже порога OBSERVE — прямое повторение
  сценария из replay-находки этапа 6, теперь как формальный тест, а не
  наблюдение в логе).

### ✅ 9b. Bot Pattern Library — ЗАВЕРШЕНО

Реализовано проще, чем в исходном черновике плана, и это осознанное
упрощение, а не недоделка: вместо `conditions_json` с процедурными условиями
и отдельного `action` на паттерн — простой набор порогов
(`required_signal_names`/`min_families`/`min_risk_score`/`min_confidence`/
`min_cluster_size`), который сопоставляется с уже готовым `Verdict`/
`ClusterInfo`, ничего не пересчитывая. `auto_enabled`-поле в схеме
сохранено на будущее (не подключено — паттерн сейчас классифицирует, но не
меняет `Sensitivity`, это было бы отдельной, более рискованной фичей).

`bot/moderation/patterns.py` — `Pattern` (frozen dataclass), `Pattern.matches()`,
`match_patterns(target, patterns) -> Pattern | None` (первый по весу
включённый паттерн; `min_cluster_size` игнорируется для `Verdict`, значим
только для `ClusterInfo`). Миграция 005 (`mod_patterns`) + миграция 006
(`ALTER TABLE mod_clusters/mod_verdicts ADD COLUMN pattern_id` — план
ошибочно предполагал, что это поле уже было добавлено раньше; на деле
`pattern_id` существовал только в `mod_actions` с этапа 7, здесь добавлена
недостающая половина). `types.py` — `ClusterInfo.pattern_id`/
`Verdict.pattern_id` (оба опциональные, `None` по умолчанию — не ломает
существующие конструкторы в `clustering.py`/остальном коде).

`store.py` — `PatternInput` (данные для создания, без `id`/`created_at`),
`create_pattern()`, `list_patterns(enabled_only=)`, `set_pattern_enabled()`,
`delete_pattern()`; `save_verdict()`/`save_cluster()` и `get_active_clusters()`/
`get_recent_verdicts()` теперь читают/пишут `pattern_id`. `engine.py` —
`ModerationEngine._patterns` (кеш в памяти, НЕ автообновляется на каждое
сообщение — только явным вызовом `reload_patterns()`, т.к. паттерны меняются
через панель редко, а перечитывать БД на каждый `observe()` было бы лишней
задержкой без пользы); сопоставление происходит после `policy.decide()` для
кластера И для готового `Verdict` отдельно, через `dataclasses.replace()`
(не пересчитывает risk/confidence/action — только подставляет `pattern_id`).

`panel/moderation_api.py` — `GET/POST /api/moderation/patterns`,
`POST .../patterns/{id}/enabled`, `POST .../patterns/{id}/delete`, все
мутирующие require ADMIN+ (не MODERATOR — создание/отключение правил
детекции ощутимо весомее разового бана). Экран `Patterns` в панели —
осознанно НЕ в этом подэтапе, только REST, чтобы API был готов раньше UI.

47/47 новых тестов (14 `test_patterns.py` — сопоставление условий по
каждому полю отдельно и вместе; 7 `test_store.py` — CRUD паттернов, порядок
по weight, roundtrip пустого `required_signal_names`; 2 `test_migrations.py`
— новая таблица и добавленные колонки; 3 `test_engine.py` — прямое
повторение сценария 50-ботовой атаки из этапов 3/5 теперь с ожидаемым
`pattern_id`, плюс проверка что БЕЗ `reload_patterns()` подстановки не
происходит и что `enabled=False` паттерн не выбирается; 6
`tests/panel/test_moderation_api.py` — роли ADMIN+ на каждом мутирующем
эндпоинте). 413/413 тестов всего проекта зелёных, ruff и mypy strict чисты.

### ✅ 9c. Attack Mode — ЗАВЕРШЕНО

Реализовано как в плане, с одним упрощением схемы: `mod_attack_mode` без
PK по `channel` — singleton-таблица с `id=1` (одна БД = один бот-инстанс =
один канал, отдельные строки на канал избыточны при текущей архитектуре).

Миграция 007 — `mod_attack_mode(id PK CHECK(id=1), activated_by,
activated_at, expires_at)`. `store.py` — `AttackModeStatus` (frozen
dataclass, `to_dict()` с вычисляемым `seconds_remaining`),
`activate_attack_mode()` (upsert через `ON CONFLICT`), `deactivate_attack_mode()`,
`get_active_attack_mode()` (сама фильтрует по `expires_at <= now` — вызывающий
код получает уже отфильтрованный по времени статус, не отдельно "запись
есть" и отдельно "но устарела").

`engine.py` — `ModerationEngine._attack_mode: AttackModeStatus | None`
(кеш в памяти, не перечитывается на каждое сообщение), `sync_attack_mode()`
(явная синхронизация, тот же паттерн, что `reload_patterns()` из 9b),
`attack_mode_active` property (дешёвое сравнение `expires_at` с текущим
временем БЕЗ похода в БД). В `observe()` — `Sensitivity.ATTACK` подставляется
в `risk_score()` вместо `self._config.sensitivity`, когда `attack_mode_active`;
`policy.decide()` не тронут — `MIN_FAMILIES_FOR_BAN` физически недостижим
отсюда, инвариант проверен отдельным тестом (один слабый сигнал не
эскалирует до BAN даже под активным Attack Mode).

`panel/moderation_api.py` — `POST /api/moderation/attack_mode/activate`
(ADMIN+, `duration_seconds` default 1800, отклоняет `<= 0`),
`POST .../deactivate` (ADMIN+), `GET .../attack_mode` (доступен всем
вошедшим — статус-баннер, не действие). Actor берётся из сессии, как везде.

**Связь бот-панель, найденная и закрытая по ходу работы (не было в
исходном плане):** Attack Mode и Pattern Library хранятся в БД, но бот
(`main.py`) и панель — разные процессы. Без явной синхронизации включение
Attack Mode через панель не доходило бы до работающего движка, пока бот не
перезапущен. Добавлен `ChatBot._poll_moderation_updates()` в `main.py` —
фоновая задача, вызывающая `reload_patterns()`/`sync_attack_mode()` каждые
10 секунд (быстрая реакция важна для panic-режима), плюс однократный вызов
обоих методов в `event_ready()` при старте бота. Оба метода — чистое
чтение БД, токена не требуют, поэтому в отличие от `executor.py` (этап 7,
до сих пор не подключён — ждёт токена) подключение было безопасно сделать
сразу.

51/51 новых тестов (7 `test_migrations.py`/схема; 7 `test_store.py` —
CRUD, upsert при повторной активации, `expires_at` в прошлом читается как
неактивно, `to_dict()`; 6 `test_engine.py` — риск выше при ATTACK на тех же
сигналах, инвариант `MIN_FAMILIES_FOR_BAN` не обходится, `sync_attack_mode()`
без store не падает, дезактивация реально отключает; 8
`tests/panel/test_moderation_api.py` — роль ADMIN+ на activate/deactivate,
default-длительность 1800с, отказ на неположительный `duration_seconds`,
actor из сессии). 435/435 тестов всего проекта зелёных, ruff и mypy strict
чисты (два mypy false positive — "unreachable"/несравнимый `object`-тип из
`dict[str, object]` — исправлены явной типизацией в тестах, не в проде).

### ✅ 9d. FP-статистика / feedback loop — ЗАВЕРШЕНО

Миграция 008 — `mod_feedback(id PK, created_at, signal_name, verdict_id,
cluster_id, user_id, pattern_id, moderator, decision)` (упрощена
относительно черновика плана: без `risk_score`/`confidence`/`signals_json`
— эти значения уже лежат в связанном `mod_verdicts`/`mod_signals` через
`verdict_id`, дублировать их в `mod_feedback` было бы избыточно) +
`mod_stats_daily` как в исходном плане, без изменений.

`store.py` — `record_feedback()`, `get_signal_fp_penalty(signal_name,
sample_size=50)` (доля FALSE_POSITIVE среди последних N решений ПО
КОНКРЕТНОМУ СИГНАЛУ — не по вердикту целиком, т.к. один вердикт часто
содержит несколько сигналов, и ошибиться модератор может насчёт одного
конкретного), `list_feedback()`, `increment_daily_stats()` (аддитивный
upsert по дате — каждый вызов ДОБАВЛЯЕТ к счётчику, не перезаписывает;
whitelist разрешённых колонок закрывает f-string SQL-инъекцию через имена
counters), `get_daily_stats()`.

`engine.py` — `reload_fp_penalties()` (тот же явный reload-паттерн, что
`reload_patterns()`/`sync_attack_mode()`: словарь `signal_name -> fp_penalty`
кешируется в памяти, пересчитывается по вызову, не на каждое сообщение —
N сигналов означало бы N SQL-запросов на каждый `observe()`). В `observe()`
берётся МАКСИМАЛЬНЫЙ fp_penalty среди сработавших сигналов текущего
вердикта (не средний — самое ненадёжное правило определяет итоговую
скидку доверия, консервативнее и соответствует духу false-positive-guard'а)
и передаётся в уже существующий параметр `compute_confidence(...,
fp_penalty=...)`.

**Связь бот-панель (продолжение находки из 9c):** `main.py`'s
`_poll_moderation_updates()` расширен — `reload_fp_penalties()` вызывается
не каждые 10 сек, как Attack Mode/Pattern Library, а раз в 10 тиков
(~100 сек): фидбек от модераторов копится медленно (решения людей, не
секундная активность), и пересчёт на каждый быстрый тик означал бы лишние
~20 SQL-запросов (по числу известных конфигу сигналов) без пользы.

`bot/moderation/report.py` (новый, был только упомянут в разделе 3 плана
раньше) — `build_report(store, days=30)` агрегирует `mod_stats_daily` +
`mod_feedback` в `ModerationReport` с `format_summary()`. `scripts/report.py`
— тонкий CLI по аналогии с `scripts/replay.py`
(`python scripts/report.py --db bot.db --days 7`). REST для панели готов
(`GET /api/moderation/feedback`, `POST /api/moderation/feedback` — требует
MODERATOR+, actor из сессии; `GET /api/moderation/stats/daily`) — экран
`Stats`, как и `Patterns`, сознательно вне этого подэтапа.

Кнопка "это был false positive" в самой панели (UI) НЕ добавлена в этом
подэтапе — только REST. Отдельно от `clusters/{id}/mark_safe`/`ignore` из
этапа 8 (те архивируют кластер целиком) и от `users/{id}/mark_safe` из
этапа 9a (доверие пользователю на будущее) — семантически третье, отдельное
действие ("система ошиблась именно в ЭТОМ сигнале"), для UI нужен экран
Live с точкой клика на конкретный сигнал, которого сейчас нет.

29/29 новых тестов (2 схема в `test_migrations.py` через существующий
tables-check; 20 `test_store.py` — feedback CRUD, точный расчёт fp_penalty
по доле, изоляция по имени сигнала, `sample_size` ограничивает выборку,
`daily_stats` аддитивность/дни/неизвестный счётчик отклоняется; 3
`test_engine.py` — без reload fp_penalty не влияет, после reload реально
снижает confidence, вызов без store не падает; 7 `test_report.py` —
агрегация по дням/окну/сигналам, сортировка по fp_rate, пустая БД не
падает; 7 `tests/panel/test_moderation_api.py` — роль MODERATOR+, отказ
на неизвестный `decision`, actor из сессии).

**Итог этапа 9 целиком: 464/464 тестов зелёных, ruff и mypy strict чисты,
main.py/panel.server по-прежнему импортируются.**

---

## Прогресс 9a (реализовано как в плане, без отклонений)

`bot/moderation/config.py` — `TrustConfig` (`enabled`, `min_messages_for_regular`,
`min_days_for_regular`, `regular_risk_multiplier`), подключён в `ModerationConfig`,
парсер, `default_config()`, `config/moderation.yml`. `bot/moderation/types.py` —
`UserState.qualifies_for_regular()` (оба порога обязательны одновременно — see
докстринг). `bot/moderation/engine.py` — `_get_or_create_user()` теперь
односторонне поднимает `UNKNOWN -> REGULAR` (никогда не понижает, не трогает
`TRUSTED`/`PRIVILEGED`), передаёт `regular_user=` в `risk_score()`.
`bot/moderation/scoring.py` — `risk_score()` получил keyword `regular_user: bool`,
применяет `trust.regular_risk_multiplier` как ДОПОЛНИТЕЛЬНЫЙ множитель поверх
`mode_multiplier` (не подменяет его). Инвариант проверен тестом: скидка REGULAR
не может увести CRITICAL-сигнал (вес 100) ниже порога TIMEOUT сама по себе.

Миграция 004 (`mod_trusted`) + `store.py`: `mark_trusted()`/`unmark_trusted()`/
`is_trusted()`/`list_trusted()` — `mark_trusted()` одновременно пишет запись
аудита в `mod_trusted` И обновляет `mod_users.marked_safe` (то поле, которое
реально читает `policy.is_protected` через `UserState.marked_safe`).
`panel/moderation_api.py` — новые эндпоинты `POST /api/moderation/users/{id}/
mark_safe`, `POST /api/moderation/users/{id}/unmark_safe`, `GET /api/moderation/
trusted`, все под `require_authenticated` + `require_role(MODERATOR+)`; actor
берётся из сессии (`login`), не из тела запроса — нельзя подделать "кто пометил".
Фронтенд — login-чипы участников кластера теперь кликабельны (hover меняет цвет
на success), клик вызывает `markUserSafe()`; кнопка "MARK SAFE" на самой карточке
кластера (архивный статус конкретного инцидента, этап 8) переименована в
"IGNORE CLUSTER" для разделения смыслов — не одно и то же, что доверие
пользователю на будущее.

382/382 тестов зелёных (27 новых: `test_types.py` — `qualifies_for_regular`
на обоих порогах по отдельности и вместе; `test_config.py` — `TrustConfig` из
дефолтов/YAML/валидация опечаток; `test_scoring.py` — скидка REGULAR применяется,
пропорциональна конфигу, отключаема, не может сама по себе погасить CRITICAL;
`test_engine.py` — `TestAutomaticTrust`: REGULAR снижает risk на органичной
копипаста-перекличке (прямое повторение replay-находки этапа 6, теперь тест, а
не наблюдение в логе) И не блокирует реальную атаку с тем же user_id;
`test_store.py`/`test_migrations.py` — CRUD `mod_trusted`, включая upsert и
попытку пометить user_id без записи в `mod_users`; `tests/panel/test_moderation_api.py`
— роли/сессия на новых эндпоинтах, actor из сессии). ruff чист, mypy strict
чист (55 файлов), `main.py`/`panel.server` по-прежнему импортируются.

## Этап 9 завершён целиком (9a→9d) — что дальше

Все 4 подэтапа реализованы, протестированы и задокументированы выше
(разделы "Прогресс 9a/9b/9c/9d"). Все пункты исходного ТЗ, которые
породили этот план, закрыты кодом: risk-scoring, детекторы, кластеризация,
БД и аудит, SHADOW-режим в проде, replay-калибровка на реальных данных,
Helix-клиент и executor (на моках, ждут токена), панель с ролями и
Twitch OAuth, Trusted Users с автодоверием по истории, Pattern Library,
Attack Mode, FP-feedback loop.

### ✅ Панельные экраны Patterns/Attack Mode/Stats + кнопка FP — ЗАВЕРШЕНО

Добавлено после закрытия этапа 9 отдельным проходом (без изменения
логики движка/REST — только фронтенд поверх уже готового API из 9b/9c/9d,
плюс один новый серверный метод для их поддержки):

- **`store.get_recent_verdicts()`** теперь возвращает `signal_names:
  list[str]` для каждого вердикта (отдельный запрос к `mod_signals` по
  `verdict_id IN (...)`, не JOIN — читаемее и не дублирует строки
  вердикта на каждый сигнал). Нужно было для кнопки FP на конкретном
  сигнале — до этого API отдавал только risk/confidence/reason, но не
  то, какие именно сигналы сработали.
- **Экран `Patterns`** (`panel/static/moderation.html`/`.js`): список
  существующих паттернов с условиями в человекочитаемом виде, форма
  создания (все поля `PatternRequest`), кнопки вкл/выкл и удаление.
  Мутации скрыты для не-ADMIN (проверка на фронте — косметика, реальная
  проверка прав как и раньше только в роутере через `require_role`).
- **Экран `Attack Mode`**: текущий статус (вкл/выкл, кем активирован,
  сколько осталось), форма активации с `duration_seconds`, кнопка
  деактивации. Плюс лёгкий пульсирующий баннер в шапке (`#attack-banner`),
  виден на любом экране панели через фоновый опрос `GET
  /api/moderation/attack_mode` раз в 15 сек — модераторы должны видеть,
  что канал в panic-режиме, даже если не открыт сам экран Attack Mode.
- **Экран `Stats`**: агрегированные плитки за 30 дней из `GET
  /api/moderation/stats/daily` (суммирование на фронте — сервер отдаёт
  посуточные записи, агрегацию по окну целиком делать на бэкенде не
  стали, т.к. `report.py`/`scripts/report.py` уже закрывают эту нужду
  для CLI, а панели пока достаточно простой суммы) + таблица FP-rate по
  сигналам, отсортированная по убыванию (те же данные, что
  `report.build_report()`, но из живого `GET /api/moderation/feedback`,
  без похода в CLI).
- **Кнопка false positive**: каждый сигнал в ленте подозрительных
  вердиктов (`#verdict-feed`) теперь кликабельный чип — клик шлёт `POST
  /api/moderation/feedback` с `decision=FALSE_POSITIVE` и всеми
  доступными ссылками (`verdict_id`/`cluster_id`/`user_id`/`pattern_id`).
  Отдельно от кнопки MARK SAFE на пользователе (9a) и IGNORE CLUSTER
  (этап 8) — это единственное действие, которое бьёт по конкретному
  сигналу, а не по пользователю/кластеру целиком, и то самое звено,
  которого не хватало, чтобы `get_signal_fp_penalty()` (9d) реально
  наполнялся данными не только через ручной вызов REST.

469/469 тестов зелёных (5 новых: 4 в `test_store.py` на
`signal_names` — присутствие, пустой список, изоляция между вердиктами,
пустая БД; 1 в `tests/panel/test_moderation_api.py` на то же поле через
REST), ruff и mypy strict чисты, `main.py`/`panel.server` по-прежнему
импортируются. UI-код (HTML/JS) не покрыт автотестами (как и остальной
JS панели с самого этапа 8) — проверен статически: синтаксис (`node
--check`), соответствие `id`-атрибутов между HTML и JS, экранирование
пользовательских строк через существующий `escapeHtml()` везде, где в
DOM подставляются данные с сервера (имена паттернов, описания, логины).

**Что осознанно осталось не сделано и почему (актуально на конец этой сессии):**

1. **Включение LIVE-режима** (этап 10 по нумерации исходного плана) — по
   решению владельца канала, на основе данных, накопленных в SHADOW.
   Единственный оставшийся пункт исходного плана.

Блокер #1 (токен с правами на баны) и оба недостающих экрана панели
(Users, Settings) закрыты в рамках этой сессии — см. раздел ниже.

### ✅ Токен модератора, executor в main.py, экраны Users/Settings — ЗАВЕРШЕНО

Закрывает блокер #1 (раздел 11) и последние два пункта из списка "что
осталось" выше — по прямому запросу пользователя ("доделать всё, что
осталось, и лично протестировать перед реальными условиями").

**Получение токена бота — через панель, без стороннего сайта.**
`panel/auth.py` получил второй, независимый OAuth-flow поверх уже
существующего (`/auth/login` для входа в панель): `GET /auth/bot/login`
(ADMIN+, `force_verify=true`, чтобы Twitch не подставил тихо уже
залогиненный в браузере аккаунт) → `GET /auth/bot/callback` — с scope
`moderator:manage:banned_users moderator:manage:chat_messages`, отдельным
`state`-хранилищем (`_pending_bot_states`, не путается с обычным входом),
собственным `redirect_uri` (`PANEL_TWITCH_BOT_REDIRECT_URI`, дефолт
`/auth/bot/callback`). Использует то же Twitch-приложение, что вход в
панель (`PANEL_TWITCH_CLIENT_ID/SECRET`) — решение пользователя, не плодить
второе приложение. Результат (access+refresh токен, bot_login, bot_user_id,
broadcaster_id) пишется в **корневой** `.env` (`TWITCH_MOD_*`) — один набор
на процесс панели, без per-profile усложнения (решение пользователя: сейчас
один бот/канал). `/auth/bot/callback` — реальный OAuth redirect target,
отдаёт HTML-страницу с результатом и предупреждением, если вошедший
аккаунт не модератор канала; `/auth/bot/callback.json` — тот же обмен, но
JSON, для тестов/программных клиентов (сама логика обмена вынесена в
`_process_bot_callback()`, используется обоими).

**Автообновление токена** — `bot/moderation/mod_token.py` (новый модуль,
живёт в процессе БОТА, не панели). `ModTokenManager.get_valid_access_token()`
обновляет токен через `refresh_token` grant заранее (за 5 минут до
истечения, не впритык), пишет новую пару обратно в `.env` при каждом
обновлении — иначе рестарт бота между refresh-циклами подхватил бы уже
отозванный Twitch access_token. `load_mod_token_manager()` возвращает
`None`, если токен ни разу не получен (`main.py` должен уметь работать без
него, не падать при старте).

**Executor подключён к `main.py`** — `ChatBot._poll_action_queue()`, новый
метод, опрос раз в 2 сек (быстрее, чем Attack Mode/Pattern Library: BAN ALL,
нажатый во время атаки, не должен ждать 10 сек). Пересоздаёт
`ActionExecutor` на каждый цикл со свежим `access_token` от
`mod_token_manager` (сам `ActionExecutor` хранит токен как фиксированную
строку на момент создания — пересоздание, а не обновление поля, самый
простой способ гарантировать актуальность без изменения уже
протестированного интерфейса `executor.py` из этапа 7). Если токен не
настроен, поллер не запускается вовсе — задания в очереди накапливаются
(панель по-прежнему может их туда класть), но не исполняются, и лог явно
объясняет почему.

**Экран `Settings`** — `bot/moderation/config.py::load_config()` уже
валидировал YAML строго (неизвестный ключ = `ConfigError`), это
переиспользовано для валидации ДО записи на диск (`POST
/api/moderation/config` пишет во временный `.yml.tmp-validate`,
валидирует, только потом перезаписывает боевой файл — битый YAML никогда
не попадает на диск). UI — гибрид: быстрые поля (`mode`, `risk_thresholds`)
поверх текстового `<textarea>` с полным YAML, синхронизируются простой
строковой заменой команд верхнего уровня, не пересборкой всего файла (не
теряет ручные правки остального конфига). Конфиг движка читается ОДИН РАЗ
при старте бота — hot-reload сознательно не делался (отдельная, более
рискованная фича: веса/пороги детекции "на лету" под живым трафиком), UI
прямо предупреждает `restart_required: true` после сохранения. Плюс
статус токена бота (настроен/не настроен, кто именно) и кнопка "Получить
токен бота", ведущая на `/auth/bot/login`.

**Экран `Users`** — `store.list_users()` (новый метод): все зрители,
отсортированные по `message_count` убыв. (самый частый порядок поиска
"кто это вообще такой"), с фильтром по подстроке логина
(`GET /api/moderation/users?search=`). Показывает `trust_level`,
`marked_safe`, `first_seen`/`last_seen`, `prior_timeouts` — то, что раньше
было видно только через прямой SQL-запрос к `mod_users`.

**Изменения существующего кода:** `store.get_recent_verdicts()` не
трогался повторно; `panel/server.py` получил `app.state.panel_root = ROOT`
(нужно `panel/auth.py` для записи `.env`).

45/45 новых тестов (10 `tests/moderation/test_mod_token.py` — refresh на
первом использовании, переиспользование в окне валидности, повторное
обновление у границы `_REFRESH_MARGIN_SECONDS`, запись обратно в `.env` без
потери остальных ключей, ошибка при отозванном refresh_token; 7
`tests/moderation/test_store.py::TestListUsers` — сортировка, поиск без
учёta регистра, limit/offset, отражение `marked_safe`; 9
`tests/panel/test_moderation_api.py::TestConfigEndpoints` — 404 без файла,
парсинг полей, отчёт об ошибке парсинга без падения, ADMIN-only на
сохранении, откат при невалидном YAML, отсутствие временного файла после
сохранения; 11 `TestUsersEndpoint`; 8 `tests/panel/test_auth.py` на
bot-token flow — 401/403 на login без сессии/с VIEWER, редирект с нужными
scope, неизвестный state, полный цикл с записью в `.env`, предупреждение
о немодераторском аккаунте, HTML-страницы успеха/ошибки).

509/509 тестов всего проекта зелёных, ruff и mypy strict чисты (61 файл в
scope), `main.py`/`panel.server` по-прежнему импортируются. UI-код
(HTML/JS) проверен статически, как и в предыдущей записи — синтаксис,
соответствие `id`, `escapeHtml()` на пользовательских строках.

**Что до сих пор не проверено — требует реального стрима, не тестов:**
живой прогон получения токена бота через настоящий Twitch (аналогично
тому, как в своё время находились реальные проблемы с OAuth-scope для
входа в панель), реальный BAN/TIMEOUT через Helix с настоящим токеном,
поведение `_poll_action_queue()` под живой нагрузкой. Следующий шаг —
именно это совместное тестирование, запрошенное пользователем.

Следующий шаг при возобновлении — по указанию пользователя: живое
тестирование перед боевым запуском (токен, реальные баны, экраны панели),
либо решение по LIVE-режиму, либо новая задача вне этого плана.

Дата аудита: 2026-08-07. Дата завершения этапа 9: 2026-08-08.
Дата добавления панельных экранов 9b/9c/9d и кнопки FP: 2026-08-08.
Дата закрытия блокера #1 (токен, executor, Users/Settings): 2026-08-08.
Проект: `II-TwitchBOT`.

## Принятые решения

| Вопрос | Решение |
|---|---|
| Канал команд панель → бот | **Очередь в SQLite** (`mod_action_queue`), бот поллит раз в 0.5 сек |
| Доступ к панели | **Локально сейчас, сеть отдельным этапом.** Роли и проверка прав — в роутере FastAPI с самого начала, чтобы включение сети меняло один слой (`current_user`), а не архитектуру. Вход для модераторов в будущем — Twitch OAuth со сверкой списка модераторов канала через Helix |
| Определение языка | **Unicode-скрипты + `lingua`** с набором ru/en/uk/pl/cs/de и порогом уверенности; ниже порога сигнал не выдаётся. Вес сигнала — 5 |
| Helix-токен | Вопрос закрывается **на этапе 7**; этапы 0–6 токена не требуют |

---

## 1. Текущая архитектура

Python 3.12 + asyncio, без фреймворка. Два независимых процесса на бота плюс общая панель.

```
main.py                 ChatBot (twitchio 2.10) — IRC-подключение к чату
  ├─ bot/config.py      Config-dataclass из .env (плоский, всё строками)
  ├─ bot/database.py    aiosqlite: viewers, recent_messages
  ├─ bot/brain.py       DeepSeek через openai SDK
  └─ bot/voice_queue.py обмен с голосовым процессом через файл

voice_main.py           отдельный процесс: микрофон → faster-whisper → файл
  ├─ bot/voice.py
  └─ bot/audio_source.py (streamlink — читает аудио стрима)

panel/server.py         FastAPI на 127.0.0.1:8765 — запуск/остановка профилей,
panel/static/index.html vanilla JS, 1226 строк, один файл, без сборки
```

**Мультипрофильность.** `BOT_ENV_FILE` + `INSTANCE` разводят несколько ботов в одной
папке: `.env.<profile>` → своя БД `bot.<instance>.db`, свои логи, своя очередь.
Панель управляет профилями через `subprocess` + pid-файлы. Любая новая подсистема
обязана уважать эту схему — иначе два бота на разные каналы перетрут данные друг друга.

**Поток сообщения сейчас** (`main.py:269`):

```
event_message → touch_viewer → log_message → handle_commands
              → _is_addressed_to_bot? → DuplicateFilter → LLM → MessageQueue → чат
```

**Что уже есть полезного для модерации:**

| Компонент | Где | Чем полезен |
|---|---|---|
| `DuplicateFilter` | `main.py:100` | Готовая идея нормализации + `difflib`. Но заточен под другое (вырезает триггер-слово), переиспользовать напрямую нельзя |
| `MessageQueue` | `main.py:139` | Образец очереди с rate-limit и единой точкой отправки — тот же паттерн нужен для Helix-действий |
| `recent_messages` | БД | **7 271 реальное сообщение** (`bot.db` 5282 + `bot.ari.db` 1989), 211 зрителей — материал для replay и калибровки |
| Парсинг IRC-тегов | `main.py:76` | Уже умеет доставать сырые теги. Оттуда же берутся `user-id`, `first-msg`, `badges` |

---

## 2. Существующие Twitch-интеграции

| Что | Как | Ограничение |
|---|---|---|
| Чтение/запись чата | twitchio 2.10, IRC | Токен со scope **`chat:read chat:edit`** — и только |
| Live-статус канала | `streamlink` в панели | Обход API, кэш 30 сек |
| Аудио стрима | `streamlink` + `av` | К модерации отношения не имеет |

**Helix API не используется нигде.** Проверено grep'ом по проекту: ни одного обращения
к `api.twitch.tv`. Класс `Config` не содержит ни `client_id`, ни `client_secret`.

Это **главный блокер**: чат-команды `/ban` и `/timeout` через IRC Twitch отключил для
сторонних клиентов в феврале 2023. Единственный способ забанить — `POST
/helix/moderation/bans`. Для него нужны:

1. User Access Token со scope **`moderator:manage:banned_users`**
   (+ `moderator:manage:chat_messages` для удаления сообщений);
2. аккаунт бота должен быть **модератором канала** (`/mod ботник` в чате);
3. `moderator_id` = user_id бота, `broadcaster_id` = user_id канала.

Библиотека это уже умеет: `twitchio.PartialUser.ban_user()` / `timeout_user()` /
`delete_chat_messages()` (`user.py:1482`, `1514`, `1300`). `Client-ID` twitchio достаёт
сам из `/oauth2/validate` — отдельно его прописывать не нужно.

---

## 3. Moderation Engine — предлагаемая архитектура

Главный принцип, который задаёт всю структуру: **Detection и Action разделены
физически, а не соглашением.** Детектор не имеет доступа к Twitch-клиенту в принципе —
у него нет такой зависимости в конструкторе.

```
IRC-событие
    ↓
ChatEvent (dataclass, всё что известно из тегов)
    ↓
Normalizer          текст → normalized + skeleton + script-профиль + ссылки + fingerprint
    ↓
SlidingWindow       окна в памяти: сообщения, пользователи, прибытия (60 сек)
    ↓
Detectors[]         каждый: (event, user_state, window) → list[Signal]   ← чистые функции
    ↓
Clustering          union-find по рёбрам схожести → Cluster[]
    ↓
ScoringEngine       Signal[] → risk_score (веса из YAML)
    ↓
ConfidenceEngine    → confidence (по независимости семейств, объёму, истории FP)
    ↓
ContextVerifier     raid / giveaway / hype / известный зритель → корректировки
    ↓
FalsePositiveGuard  правила A–G, право вето
    ↓
PolicyEngine        → Verdict{action, reason, explain}    ← НИЧЕГО не исполняет
    ↓
    ├─→ AuditStore          пишет всегда, в любом режиме
    ├─→ Dashboard (панель)   показывает кластер + кнопки
    └─→ ActionExecutor       исполняет ТОЛЬКО по команде (auto-rule или модератор)
```

### Структура файлов

```
bot/moderation/
    __init__.py
    types.py           ChatEvent, UserState, Signal, SignalFamily, Verdict, Cluster, Action
    normalize.py       нормализация текста, Unicode-скрипты, гомоглифы, ссылки, MinHash
    window.py          скользящие окна в памяти (ring buffer, без БД)
    features.py        извлечение признаков пользователя (account_age, first_msg, badges)
    detectors/
        __init__.py    реестр детекторов
        base.py        протокол Detector
        burst.py       скорость сообщений (юзер + канал)
        duplicate.py   дубликаты и near-duplicate
        links.py       ссылки, домены, шортенеры
        unicode.py     гомоглифы, невидимые символы, script-микс
        language.py    определение языка (слабый сигнал!)
        account.py     возраст аккаунта, первое сообщение, отсутствие истории
        username.py    паттерны ников (user12345678)
        emote.py       эмодзи/эмоут-спам
    clustering.py      union-find, MinHash-схожесть, валидация кластера
    scoring.py         Signal[] → risk_score
    confidence.py      → confidence
    context.py         состояние канала: raid/giveaway/hype/обычный
    protection.py      FalsePositiveGuard (правила A–G) + trusted-users
    patterns.py        Bot Pattern Library, known_bot_patterns из YAML
    policy.py          PolicyEngine: verdict → recommended_action, режимы SAFE/BALANCED/AGGRESSIVE/ATTACK
    engine.py          оркестратор (единственная точка входа: observe(event) → Verdict)
    config.py          загрузка YAML + профиль канала + валидация + версионирование
    store.py           запись вердиктов/кластеров/аудита (батчами, в фоне)
    executor.py        Helix-действия: очередь, rate-limit, retry, частичные ошибки
    twitch_api.py      тонкий Helix-клиент: ban, timeout, delete, users lookup, кэш
    replay.py          прогон исторического лога через движок
    report.py          статистика shadow-режима и FP по правилам

config/
    moderation.yml               общие настройки и веса
    channels/<channel>.yml       профиль конкретного канала

tests/moderation/
    conftest.py, factories.py    генераторы сценариев чата
    test_normalize.py
    test_detectors_*.py
    test_clustering.py
    test_scoring.py
    test_protection.py           ← самые важные тесты
    test_policy.py
    test_engine_scenarios.py     сквозные сценарии
    test_replay_regression.py    прогон на реальных 7271 сообщениях
```

Ядро (`types` → `policy`) **не импортирует ни aiosqlite, ни httpx, ни twitchio**.
Оно синхронное и детерминированное: одинаковый вход → одинаковый вердикт. Это то,
что делает систему тестируемой и позволяет replay.

---

## 4. Детекторы

Каждый детектор возвращает `Signal{name, family, weight, value, evidence}`.
`evidence` — конкретные наблюдаемые факты для объяснения («14 сообщений за 10 сек»),
без него сигнал не принимается.

**Семейства (`SignalFamily`)** — критичны для расчёта confidence: сигналы одного
семейства не считаются независимыми подтверждениями.

| Семейство | Детекторы |
|---|---|
| `CONTENT` | duplicate, links, emote |
| `TIMING` | burst, synchronized_arrival |
| `IDENTITY` | account_age, first_message, username_pattern |
| `ENCODING` | homoglyph, invisible_chars, script_mix, language |
| `NETWORK` | cluster_membership, shared_link_group |
| `HISTORY` | prior_warnings, prior_timeouts |

### Список

| Детектор | Сигналы | Вес (BALANCED) | FP-риск |
|---|---|---|---|
| `burst` | `user_message_burst`, `channel_message_burst` | 15 / 10 | высокий (hype) |
| `duplicate` | `exact_duplicate`, `near_duplicate`, `skeleton_match` | 25 / 20 / 15 | средний |
| `links` | `link_in_first_message`, `shared_link_multi_user`, `url_shortener`, `known_scam_domain` | 10 / 30 / 10 / 35 | низкий |
| `unicode` | `invisible_chars`, `homoglyph_mix`, `script_mix_in_word` | 25 / 20 / 10 | **очень низкий** |
| `language` | `unexpected_language` | **5** | **очень высокий** |
| `account` | `new_account`, `first_message`, `no_history`, `account_age_cluster_match` | 10 / 5 / 5 / 15 | высокий |
| `username` | `generated_username_pattern` | 8 | высокий |
| `emote` | `emote_spam`, `zalgo_text_spam` | 10 / 8 | средний |
| `clustering` | `synchronized_arrival`, `cluster_membership`, `mass_first_messages` | 25 / 25 / 20 | низкий |

**Обоснование низких весов.** `unexpected_language` = 5 баллов — это ниже любого порога
действия. Один только язык не даст даже уровня «наблюдать». Он способен что-то изменить
только внутри кластера, где к нему добавляются 5–6 независимых сигналов. Ровно то, что
требуется в ТЗ (п. 13 и п. 23).

### Уточнение по языку — инженерное возражение

Определение языка по сообщению из 2–5 слов ненадёжно: `langdetect` ошибается на коротких
текстах в 30–50% случаев, а на славянских языках систематически путает ru/uk/bg и pl/cs/sk.
Строить на этом сигнал — значит закладывать FP в фундамент.

Что предлагаю вместо этого (и это сильнее):

1. **Unicode-script профиль** — доли кириллицы/латиницы/CJK/эмодзи. Дёшево, детерминировано.
2. **Mixed-script внутри слова** (`пpивет` с латинской `p`) — почти не встречается у живых
   людей, зато типичен для обхода фильтров. Сильный технический сигнал.
3. **Невидимые символы** (zero-width, RTL-override, теги) — сигнал ещё сильнее.
4. Язык — через `lingua` (точнее langdetect на коротких текстах) с ограниченным набором
   ru/en/uk/pl/cs/de и обязательным порогом уверенности; при низкой уверенности сигнал
   **не выдаётся вообще**. Вес — 5.

Важно: **латиница в русском чате — норма** (ники, `gg`, `pog`, транслит), поэтому сама по
себе латиница веса не имеет.

---

## 5. Risk Score

```python
risk = clamp(0, 100, Σ (signal.weight × signal.value × rule.weight_multiplier))
```

`signal.value` ∈ [0, 1] — насколько выражен признак (например, burst 6 msg/5s → 0.4,
20 msg/5s → 1.0). Это даёт плавность вместо ступенек if/else.

**Пороги (из конфига):**

| Диапазон | Уровень | Действие |
|---|---|---|
| 0–29 | LOW | ничего |
| 30–59 | MEDIUM | OBSERVE — попадает в панель, в аудит, повышенный мониторинг |
| 60–79 | HIGH | TIMEOUT (авто — только если разрешено И confidence ≥ порога) |
| 80–100 | CRITICAL | BAN — **только через подтверждение модератора**, кроме явно включённых known-patterns |

### Confidence — считается честно, а не «на глаз»

```
confidence = family_factor × sample_factor × (1 − fp_penalty) × context_factor
```

- `family_factor` — сколько **независимых семейств** сработало: 1 → 0.45, 2 → 0.70,
  3 → 0.88, 4 → 0.95, 5+ → 0.98. Два сигнала из одного семейства уверенность не удваивают.
- `sample_factor` — объём наблюдений: 1 сообщение / кластер из 3 → 0.6; 10 сообщений /
  кластер из 15 → 1.0.
- `fp_penalty` — исторический false-positive rate сработавших правил из `mod_feedback`.
  Если модераторы 18% раз помечали срабатывание `foreign_first_message` как ошибку,
  confidence по нему умножается на 0.82. **Система учится на кнопке MARK AS SAFE.**
- `context_factor` — 0.5 во время raid/giveaway, 0.8 при hype, 1.0 в обычном режиме.

**Жёсткое правило в коде** (не в конфиге, изменить из панели нельзя):
BAN невозможен при `families_triggered < 2`. Один признак не банит никогда — это
инвариант, закрытый тестом.

---

## 6. Cluster Detection

Онлайн-кластеризация в скользящем окне (60 сек по умолчанию).

**Шаг 1. Отпечаток сообщения.**
- `minhash` — 64 хеша от символьных 3-грамм нормализованного текста (устойчиво к мелким
  правкам, `Jaccard ≈ доля совпавших хешей`, считается за микросекунды, без зависимостей);
- `skeleton` — структурный шаблон: буквы→`a`, цифры→`9`, эмодзи→`e`. Ловит
  «Привет 123» / «Привет 456»;
- `domains` — нормализованные домены ссылок;
- `script_profile`.

**Шаг 2. Ребро между пользователями A и B**, если совпало хоть что-то:
- `jaccard(minhash) ≥ 0.75`, или
- одинаковый домен ссылки, или
- одинаковый `skeleton` при длине ≥ 12 символов.

**Шаг 3.** Union-find → компоненты связности = кандидаты в кластеры.

**Шаг 4. Валидация кластера** (все условия обязательны):
- `size ≥ min_users` (4);
- медианное окно прибытия ≤ `arrival_window` (20 сек);
- доля первых сообщений / новых аккаунтов ≥ `new_ratio` (0.5).

**Шаг 5. Стоп-лист частых фраз — ключевая защита от ложных кластеров.**
Обычные зрители массово пишут одинаковое: `+`, `ахахах`, `GG`, `привет`, спам-эмоуты.
Топ-N частых нормализованных фраз канала (строится автоматически из истории —
у нас уже есть 7271 сообщение) **не создаёт рёбер**. Без этого механизма любой
хайп-момент превращается в «кластер».

Выход:

```json
{
  "cluster_id": 42, "size": 12, "similarity_score": 0.94,
  "arrival_window_sec": 8, "account_age_similarity": "high",
  "confidence": 0.97, "signals": ["synchronized_arrival", "near_duplicate", "shared_link_multi_user"]
}
```

---

## 7. Moderator Dashboard

**Отдельная страница `/moderation`, а не вкладка в существующем `index.html`.**
Причина: текущий `index.html` — 1226 строк с инлайн-скриптом; дописывать туда дашборд
модерации значит рисковать работающим UI управления ботами. Новая страница переиспользует
CSS-переменные и стиль, но живёт в своём файле.

```
panel/static/moderation.html     дашборд
panel/static/moderation.js       логика (vanilla JS, как в проекте — без сборки)
panel/moderation_api.py          роутер FastAPI, подключается к существующему app
```

**Главный экран — «реакция за секунды»:** список активных кластеров, отсортированных по
риску, каждый — карточка с сигналами и кнопками `BAN ALL` / `TIMEOUT ALL` / `VIEW` /
`MARK SAFE` / `IGNORE`. Подтверждение — модалка с точным числом пользователей.
Обновление — WebSocket (`/ws/moderation`), не polling: при атаке задержка критична.

Экраны: `Live` (кластеры + поток подозрительных), `Users` (карточка пользователя со
всей историей сигналов), `Clusters` (архив), `Audit` (кто что нажал), `Patterns`
(библиотека паттернов), `Settings` (режим, языки, детекторы, авто-действия, права),
`Stats` (shadow-статистика, FP по правилам).

**Как кнопка в панели доходит до бота.** Панель — отдельный процесс, Twitch-подключение
есть только у процесса бота. Предлагаю **очередь команд в SQLite** (`mod_action_queue`):
панель пишет задание, бот поллит раз в 0.5 сек, исполняет, пишет прогресс обратно.

Почему не HTTP-сервер внутри бота: профилей несколько, каждому нужен свой порт →
конфликты портов и лишняя конфигурация; очередь в БД переживает рестарт панели и
бесплатно даёт аудит. 0.5 сек задержки на бан несущественны.

**Роли.** `OWNER` / `ADMIN` / `MODERATOR` / `VIEWER` — таблица `mod_panel_users`,
проверка прав в роутере, а не в UI. Оговорка: панель сейчас слушает `127.0.0.1` **без
какой-либо авторизации**. Пока это так, роли — модель данных, а не защита. Вопрос
вынесен в раздел 11.

---

## 8. Database schema

Новые таблицы в той же `bot.<instance>.db`. Существующие `viewers` / `recent_messages`
**не трогаем** — `brain.py` и панель на них завязаны. Добавляем только индексы.

```sql
-- Пользователи (по user_id, а не по нику: ники меняются, user_id — нет)
mod_users(user_id PK, login, display_name, account_created_at, first_seen, last_seen,
          message_count, trust_level, is_watchlisted, marked_safe_at, marked_safe_by, note)

-- Сообщения с признаками (для replay и разбора «почему»)
mod_messages(id PK, user_id, login, text, normalized, skeleton, minhash, ts,
             is_first_msg, is_returning, badges, is_sub, is_mod, is_vip,
             script_profile, lang, lang_confidence, domains)

mod_verdicts(id PK, ts, user_id, message_id, risk_score, confidence, families_count,
             recommended_action, action_taken, reason, cluster_id, mode,
             engine_version, config_version)

mod_signals(id PK, verdict_id FK, name, family, weight, value, evidence)

mod_clusters(id PK, created_at, closed_at, size, similarity_score, arrival_window_sec,
             risk_score, confidence, signals_json, pattern_id, status)
             -- status: active | actioned | ignored | marked_safe | expired

mod_cluster_members(cluster_id FK, user_id, message_id, joined_at, PRIMARY KEY(cluster_id,user_id))

-- Кто что нажал. Главный ответ на вопрос «кто забанил 17 человек и по какому правилу»
mod_actions(id PK, ts, actor, actor_role, action, scope, target_user_id, cluster_id,
            pattern_id, reason, confirmation, result, succeeded, failed, details_json)
            -- action: BAN|TIMEOUT|DELETE|UNBAN|MARK_SAFE|WATCHLIST|IGNORE
            -- scope: user|cluster|type ; confirmation: AUTO|MANUAL

-- Канал управления панель → бот
mod_action_queue(id PK, created_at, requested_by, requested_role, payload_json,
                 status, progress_done, progress_total, started_at, finished_at, result_json)

-- Обучение на ошибках
mod_feedback(id PK, ts, verdict_id, cluster_id, user_id, pattern_id, moderator,
             decision, risk_score, confidence, signals_json)
             -- decision: FALSE_POSITIVE | CONFIRMED_BOT

mod_trusted(user_id PK, added_by, added_at, reason)
mod_panel_users(login PK, role, token_hash, created_at, last_seen)
mod_patterns(id PK, name, description, conditions_json, action, min_confidence,
             enabled, auto_enabled, weight, created_by, created_at, stats_json)
mod_stats_daily(date PK, total_messages, suspicious, would_timeout, would_ban,
                actual_timeouts, actual_bans, clusters, false_positives)
```

Индексы: `mod_messages(ts)`, `mod_messages(user_id, ts)`, `mod_verdicts(ts)`,
`mod_verdicts(user_id)`, `mod_cluster_members(user_id)`, `mod_actions(ts)`,
`mod_action_queue(status)`, плюс `recent_messages(created_at)` для существующей таблицы.

**Миграции** — версионированные, идемпотентные, `CREATE TABLE IF NOT EXISTS` +
`PRAGMA user_version`. Существующие БД (`bot.db`, `bot.ari.db`) должны открыться
без потери данных.

---

## 9. Новые файлы

Всё перечислено в разделе 3 плюс:

```
config/moderation.yml                  общие настройки
config/channels/example.yml            шаблон профиля канала
docs/moderation-plan.md                этот документ
docs/moderation-runbook.md             что делать во время атаки (для модератора)
bot/moderation/**                      ~22 модуля (раздел 3)
panel/moderation_api.py                REST + WebSocket роутер
panel/static/moderation.html/.js/.css  дашборд
tests/moderation/**                    ~12 тестовых модулей
scripts/replay.py                      CLI: прогон исторического лога
pyproject.toml                         конфиг ruff/pytest/mypy (сейчас его нет)
```

## 10. Изменяемые существующие файлы

| Файл | Изменение | Риск |
|---|---|---|
| `main.py` | В `event_message` — один вызов `await moderation.observe(event)` в `try/except`, чтобы сбой модерации не ронял бота. Сбор IRC-тегов (`user-id`, `first-msg`, `badges`). Плюс: не отвечать LLM пользователям с высоким риском | низкий |
| `bot/config.py` | Новые поля: `moderation_enabled`, `moderation_mode`, `moderation_config_path`, `twitch_client_id`, `twitch_client_secret`, `helix_token`. Все — с дефолтами, обратная совместимость сохраняется | низкий |
| `bot/database.py` | Миграции, индексы, WAL, батч-коммит. **Существующие методы не меняются** | средний |
| `panel/server.py` | `app.include_router(moderation_router)` — 2 строки | низкий |
| `panel/static/index.html` | Ссылка на `/moderation` в сайдбаре — 1 строка | низкий |
| `requirements.txt` | `PyYAML`, `lingua-language-detector` (опц.), dev: `pytest`, `pytest-asyncio`, `ruff`, `mypy` | низкий |
| `.env.example`, `README.md` | Документация новых переменных и получения токена с moderator-scope | нулевой |

---

## 11. Проблемы и ограничения

### Блокеры (без решения система не сможет банить)

1. ✅ **Scope токена — РЕШЕНО.** Токен чата (`chat:read chat:edit`) остаётся
   отдельным от токена модерации: `panel/auth.py::/auth/bot/login` выпускает
   отдельный User Access Token со scope `moderator:manage:banned_users
   moderator:manage:chat_messages` через браузерный OAuth (не сторонний
   сайт-генератор), хранится в `.env` как `TWITCH_MOD_*`, автообновляется
   (`bot/moderation/mod_token.py`). Получение — кнопка в Settings панели,
   требует роль ADMIN+.
2. ✅ **Бот должен быть модератором канала — учтено, не автоматизируемо.**
   `/auth/bot/callback` проверяет это сразу после получения токена (через
   `GET /helix/moderation/channels`) и явно предупреждает в ответе, если
   аккаунт, под которым вошли, не модератор — Helix иначе вернёт 401 при
   первом же реальном бане. Сама выдача прав (`/mod <бот>` в чате) остаётся
   ручным шагом владельца канала — программно её не выполнить.
3. **Чат-команды `/ban` не работают** с февраля 2023 — только Helix. Не
   актуально: система с самого этапа 7 построена на Helix, не на IRC-командах.

### Ограничения Twitch API

4. **Ban User не батчевый** — один запрос на одного пользователя. Кластер из 50 = 50
   запросов. Отсюда: очередь с прогрессом, честный отчёт `14/17 banned, 3 failed`
   с причиной по каждому — ровно как в ТЗ (п. 7).
5. **Rate limits.** Общий лимит Helix — 800 points/min на client_id; отдельный лимит для
   банов Twitch публично не документирует. Значит: консервативный дефолт (~10 запросов/сек,
   настраиваемо), чтение заголовков `Ratelimit-Remaining` / `Ratelimit-Reset`,
   экспоненциальный backoff с джиттером на 429/5xx.
6. **Возраст аккаунта не приходит в IRC.** Нужен `GET /helix/users` (батч до 100 логинов).
   Отсюда — **двухфазная оценка**: первичный вердикт выносится мгновенно по данным из
   тегов, а при получении `created_at` пересчитывается. Архитектурно заложено сразу
   (`Verdict.is_provisional`), иначе потом переписывать движок.
7. **Follower-статус** требует `moderator:read:followers` и отдельного запроса на
   пользователя — дорого при атаке. Кэшировать, при недоступности сигнал не выдавать.
8. **`first-msg` тег** Twitch присылает бесплатно и надёжно (`first-msg=1`) — самый дешёвый
   сильный сигнал, но только в реальном времени: в исторических данных его нет.
9. **Бан не отменяет прочитанного.** Сообщения атаки останутся в чате, если не удалить их
   отдельно (`delete_chat_messages`). Учесть в BAN ALL.

### Проблемы в текущем коде

10. **`db.log_message` делает `commit()` на каждое сообщение.** При атаке в сотни
    сообщений в секунду это упрётся в fsync и станет узким местом всего бота.
    Лечится WAL + батч-коммитом.
11. **Нет `user_id` в БД.** `viewers` ключуется по нику, а Helix банит по числовому id;
    ники меняются, история теряется. Новая схема ведётся по `user_id`.
12. **`recent_messages` растёт бесконечно** — нет ни ретенции, ни индекса по времени.
13. ✅ **Панель без авторизации — РЕШЕНО.** Изначально роль была тем, что клиент
    заявлял о себе заголовком `X-Panel-Role`, без проверки — рабочая заглушка на
    период "панель только на 127.0.0.1", но не защита, если панель доступна кому-то
    ещё в локальной сети. Закрыто через Twitch OAuth (`panel/auth.py`) уже после
    этапа 8, по прямому запросу — см. отдельную запись ниже.
14. **Нет тестов, линтера, typecheck, CI.** Требование «после каждого этапа запускать
    tests/lint/typecheck/build» невыполнимо, пока инфраструктуры нет → это этап 0.
    «Build» для Python отсутствует как понятие; заменяю на smoke-проверку импорта и старта.
15. **`DuplicateFilter` может сыграть против нас:** при бот-атаке зрители-боты, пишущие
    триггер-слово, заставят бота им отвечать. Интеграция с модерацией это чинит
    (не отвечать пользователям с risk ≥ порога) — приятный побочный эффект.

### Продуктовые риски

16. **Attack Mode снижает пороги — это по определению повышает FP.** Поэтому: только
    ручное включение OWNER/ADMIN, автоматический таймер выключения (30 мин по умолчанию),
    и запрет на снижение инварианта «минимум 2 независимых семейства для BAN».
17. **Калибровка требует данных.** Веса в этом плане — обоснованные, но стартовые.
    Реальные значения даст shadow-режим за несколько дней + replay на истории.

---

## 12. Этапы реализации

Порядок выбран так, что **способность банить появляется последней** — сначала система
учится наблюдать и объяснять.

| # | Этап | Проверка | Риск для работающего бота |
|---|---|---|---|
| 0 | Инфраструктура: `pyproject.toml`, pytest, ruff, mypy, docs | тесты запускаются | нет — код бота не тронут |
| 1 | Ядро: `types`, `normalize`, `window` + тесты | unit | нет |
| 2 | Детекторы + `scoring` + `confidence` + тесты | unit | нет |
| 3 | Кластеризация + сценарии (50 ботов / 10 поляков) | сценарные тесты | нет |
| 4 | Схема БД, миграции, `store` | миграция на копии `bot.db` | низкий |
| 5 | Подключение к `main.py` в **SHADOW** (только наблюдает и пишет) | бот работает как раньше | низкий |
| 6 | Replay CLI + прогон на 7271 реальном сообщении, калибровка | 0 авто-банов на истории | нет |
| 7 | Helix-клиент + executor + очередь (авто-действия **выключены**) | тест на моках | средний |
| 8 | Панель: дашборд, кластеры, кнопки, роли, аудит | ручная проверка | низкий |
| 9 | Bot Pattern Library, Attack Mode, trusted, FP-статистика | сценарные тесты | низкий |
| 10 | Включение авто-действий — по решению владельца, на данных из shadow | — | по решению владельца |

### Стратегия тестирования

- **Unit** — на каждый детектор, нормализацию, scoring, confidence.
- **Сценарные** — генераторы: `normal_chat()`, `hype_burst()`, `raid()`,
  `bot_attack(n=50, window_sec=20)`, `foreign_language_viewers(n=10, lang="pl")`,
  `copypaste_spam()`, `emote_spam()`.
- **Инвариантные (главные)** — закрывают требования безопасности из ТЗ:
  - 10 польскоязычных зрителей → **ни одного timeout/ban**;
  - один сигнал любой силы → **никогда не BAN**;
  - хайп на 200 сообщений в минуту → **не кластер**;
  - raid → пороги подняты, массовых действий нет;
  - trusted-пользователь → не попадает в массовое действие;
  - AGGRESSIVE и ATTACK MODE → инвариант «2+ семейства» держится.
- **Регрессия на реальных данных** — прогон `bot.db` (5282 сообщения) и `bot.ari.db`
  (1989): ожидаемый результат — **ноль рекомендаций BAN**. Любое срабатывание разбирается
  вручную как потенциальный FP.
- **Моки Helix** — executor тестируется на 429/500/частичных отказах, без сети.

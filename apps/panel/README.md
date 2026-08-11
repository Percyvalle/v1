# Панель

Веб-панель обоих проектов монорепо: анти-спам модерация и управление
нейроботами. Один процесс, один порт, один вход.

```powershell
cd apps\panel
..\..\.venv\Scripts\python -m panel.server
```

Откроется на `http://localhost:8766/`.

| Экран | Путь | Что там |
|---|---|---|
| Модерация | `/moderation` (и `/`) | кластеры, вердикты, аудит, паттерны, Attack Mode, Settings |
| Боты | `/bots` | профили ботов, промты, голос, баланс DeepSeek, логи |

Вход через Twitch: владелец канала получает роль OWNER, модераторы канала —
MODERATOR, остальные — VIEWER. ADMIN назначается вручную существующим
OWNER/ADMIN на экране Panel Users. Список ADMIN один на оба экрана.

## Почему панель одна

Экранов было два, и жили они в двух процессах — 8765 (боты) и 8766
(модерация). Порт входит в origin с точки зрения браузера, поэтому cookie
одного процесса не видна другому даже на localhost: вход приходилось
проходить дважды. Взамен разделение не давало ничего — оба экрана всё равно
поднимались и падали бы вместе с одним uvicorn.

Разделены остались **движки**, и вот это осмысленно: падение движка
модерации не должно ронять чтение IRC, а очередь `mod_inbox` между ними
даёт backpressure. Граница проходит по `main.py` и `consumer.py`, а не по
панелям.

## Устройство

```
panel/server.py        — сборка приложения: роутеры, сессия, supervisor в lifespan
panel/paths.py         — три корня (repo/bot/cigilbot) и PanelRoots
panel/auth.py          — вход через Twitch OAuth, роли, токен бота
panel/moderation_api.py — REST+WS модерации (/api/moderation)
panel/registry_api.py  — Channel Registry (/api/registry)
panel/bots_api.py      — экран ботов (/bots, /api/*), бывший apps/panel/panel/bots_api.py
panel/static/          — moderation.html/.js и index.html
scripts/merge_panel_admins.py — разовый перенос panel_admins -> mod_panel_users
```

Панель лежит вне обоих проектов и импортирует из обоих, поэтому
`panel/__init__.py` кладёт `apps/twitch-bots` и `apps/cigilbot` в
`sys.path`. Бутстрап именно в `__init__` пакета, а не в `server.py`: тесты
импортируют роутеры напрямую, минуя `server`, и иначе каждому conftest
пришлось бы чинить пути заново.

Поскольку панель больше не внутри проекта, `Path(__file__).parent.parent`
перестал означать «корень проекта». Корней теперь три, и они разные —
см. `panel/paths.py`. Самая опасная пара: `_write_env_values` пишет
`TWITCH_MOD_*` в корневой `.env`, а `cigilbot/consumer.py` читает их
оттуда же. Разъедутся — панель отрапортует об успешно полученном токене, а
все баны начнут падать с 401.

## Разработка

```powershell
cd apps\panel
..\..\.venv\Scripts\pytest
..\..\.venv\Scripts\ruff check .
..\..\.venv\Scripts\mypy
```

`panel/bots_api.py` исключён из mypy: это непроаннотированный код, который
и в своём проекте не проверялся. Остальное — `strict`.

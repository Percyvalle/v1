"""Объединённая панель. Здесь же — единственное место, где собирается
sys.path для импорта соседних проектов.

Панель импортирует и bot.* (apps/twitch-bots), и cigilbot.* (apps/cigilbot),
а сама живёт в apps/panel — ни один из двух проектов не является для неё
"своим" каталогом, откуда импорт заработал бы сам. Раньше такой проблемы не
было: каждая панель лежала внутри своего проекта и обходилась одной строкой
`sys.path.insert(0, ROOT)` в шапке.

Бутстрап стоит в __init__ пакета, а не в server.py, намеренно: `import
panel.<что угодно>` сначала выполняет этот файл, поэтому пути оказываются на
месте до того, как panel/moderation_api.py дойдёт до своего `from cigilbot...`
на уровне модуля. Если бы бутстрап жил в server.py, тесты (которые
импортируют роутеры напрямую, минуя server) падали бы на ImportError, и
каждому conftest пришлось бы чинить sys.path заново.

Порядок append, а не insert(0): подменять стандартные и установленные пакеты
одноимёнными каталогами из репозитория незачем, а вот наоборот — легко (в
apps/twitch-bots лежит пакет с именем bot, достаточно частым, чтобы
столкнуться с чужим).
"""

from __future__ import annotations

import sys

from panel.paths import BOT_ROOT, CIGILBOT_ROOT

for _project_root in (BOT_ROOT, CIGILBOT_ROOT):
    _path = str(_project_root)
    if _path not in sys.path:
        sys.path.append(_path)

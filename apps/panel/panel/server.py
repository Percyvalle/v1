"""Единственный процесс панели: экран модерации + экран нейроботов.

История этого файла — круг. Сначала /moderation была экраном внутри панели
нейроботов (порт 8765) с общей cookie-сессией. Потом её вынесли в отдельный
процесс на 8766, чтобы двумя продуктами можно было управлять независимо;
ценой стало то, что вход приходилось проходить дважды — порт входит в origin,
и cookie одного процесса не видна другому даже на localhost. Теперь экраны
снова в одном приложении, и вход снова один.

Что осталось от разделения и почему это не откат в чистом виде: движки
по-прежнему живут в отдельных процессах (main.py, consumer.py) и общаются
через mod_inbox. Объединена ровно панель — то есть тот слой, где разделение
стоило двух логинов и ничего не давало взамен, потому что оба экрана всё
равно падали бы вместе с одним и тем же uvicorn. Изоляция, ради которой
проекты разнесены (падение движка модерации не должно ронять чтение IRC),
живёт на границе процессов движков, а не панелей.

Запуск: ..\\..\\.venv\\Scripts\\python -m panel.server
Откроется на http://localhost:8766/
"""

import asyncio
import hashlib
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

# panel/__init__.py уже положил apps/twitch-bots и apps/cigilbot в sys.path —
# без этого импорты ниже не разрешились бы (см. докстринг там же).
from cigilbot.store import ModerationStore
from cigilbot.supervisor import supervisor_loop
from panel.auth import load_panel_auth_config
from panel.auth import router as auth_router
from panel.bots_api import router as bots_router
from panel.moderation_api import router as moderation_router
from panel.paths import CIGILBOT_ROOT, ENV_FILE, MAIN_PROFILE, PanelRoots
from panel.registry_api import router as registry_router

STATIC_DIR = Path(__file__).parent / "static"


def db_path(profile: str) -> Path:
    """mod_panel_users (ADMIN-оверрайды ролей) хранится в mod.db Cigilbot.

    Эта же БД теперь обслуживает и экран нейроботов: до слияния он держал
    свой список админов в panel_admins внутри bot.db, и выданный там ADMIN
    не действовал на экране модерации. Победила mod.db — bot.db принадлежит
    боту и пересоздаётся им на каждом старте через executescript без
    версионирования, тогда как здесь есть настоящие миграции."""
    return CIGILBOT_ROOT / f"mod.{profile}.db" if profile != MAIN_PROFILE else CIGILBOT_ROOT / "mod.db"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Фоновая задача внутри процесса панели, не отдельный OS-процесс — см.
    # докстринг cigilbot/supervisor.py про компромисс этого решения.
    task = asyncio.create_task(supervisor_loop(str(CIGILBOT_ROOT / "registry.db")))
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(moderation_router)
app.include_router(registry_router)
app.include_router(bots_router)


def _read_own_env(key: str) -> str:
    if not ENV_FILE.exists():
        return ""
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            return stripped.split("=", 1)[1]
    return ""


# Единственный .env на весь монорепо. Раньше их было два — по одному на
# проект, с разными значениями одних и тех же PANEL_TWITCH_* ключей, потому
# что двум процессам на разных портах нужны были разные redirect URI. Порт
# один — приложение Twitch одно — файл один (см. panel/paths.py).
_panel_auth_config = load_panel_auth_config(ENV_FILE.parent)

_session_secret = os.environ.get("PANEL_SESSION_SECRET", "") or _read_own_env("PANEL_SESSION_SECRET")
if not _session_secret:
    # Временный секрет, если вход ещё не настроен — auth_login всё равно
    # отдаст 503 без PANEL_TWITCH_* (см. PanelAuthConfig.configured),
    # роли/действия защищены require_role* независимо от секрета сессии.
    _session_secret = secrets.token_hex(32)

app.state.panel_auth_config = _panel_auth_config
app.state.panel_roots = PanelRoots.default()
app.state.moderation_store_factory = lambda: ModerationStore(str(db_path(MAIN_PROFILE)))
app.add_middleware(SessionMiddleware, secret_key=_session_secret, same_site="lax")
app.include_router(auth_router)


def _static_hash(filename: str) -> str:
    """Короткий хэш содержимого статического файла для cache-busting
    query-параметра (?v=<hash>). Правки в moderation.js/.css без этого
    зависали в HTTP-кэше браузера даже после жёсткой перезагрузки
    (Ctrl+Shift+R), потому что путь /static/moderation.js не менялся —
    браузер валидирует по ETag/Last-Modified, не всегда надёжно на
    localhost. Хэш меняется вместе с содержимым, так что URL меняется
    вместе с ним — старая закэшированная копия просто никогда не
    запрашивается повторно под новым URL."""
    return hashlib.sha256((STATIC_DIR / filename).read_bytes()).hexdigest()[:10]


_STATIC_VERSIONS = {name: _static_hash(name) for name in ("moderation.js",)}


@app.get("/")
@app.get("/moderation")
def moderation_page() -> HTMLResponse:
    html = (STATIC_DIR / "moderation.html").read_text(encoding="utf-8")
    for filename, version in _STATIC_VERSIONS.items():
        html = html.replace(f'src="/static/{filename}"', f'src="/static/{filename}?v={version}"')
    return HTMLResponse(html)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8766)

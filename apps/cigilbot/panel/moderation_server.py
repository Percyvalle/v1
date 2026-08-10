"""Отдельный процесс панели модерации — независимый от panel/server.py
(панели управления нейроботами).

Изначально /moderation была экраном внутри общего FastAPI-приложения
panel/server.py (порт 8765), с общей cookie-сессией на оба экрана. По
явному запросу пользователя разнесено на два самостоятельных процесса:
нейроботы и модерация — разные по смыслу продукты (болтливый AI-компаньон
vs анти-спам движок), которые не должны требовать одного входа, чтобы
управлять одним без другого. Цена этого решения: вход через Twitch теперь
нужно проходить отдельно на каждом порту (8765 и 8766) — cookie одного
процесса не видна другому, даже на localhost, потому что это разные
origin с точки зрения браузера (порт — часть origin).

panel/auth.py и panel/moderation_api.py уже были самостоятельными модулями
без зависимости на panel/server.py (проверено перед этим разделением) —
поэтому этот файл не дублирует их код, а просто собирает уже готовые
роутеры в свой собственный FastAPI app с собственным SessionMiddleware.

Запуск: .venv\\Scripts\\python -m panel.moderation_server
Откроется на http://localhost:8766/moderation
"""

import asyncio
import hashlib
import os
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cigilbot.store import ModerationStore  # noqa: E402
from cigilbot.supervisor import supervisor_loop  # noqa: E402
from panel.auth import load_panel_auth_config  # noqa: E402
from panel.auth import router as auth_router  # noqa: E402
from panel.moderation_api import router as moderation_router  # noqa: E402
from panel.registry_api import router as registry_router  # noqa: E402

MAIN_PROFILE = "main"


def db_path(profile: str) -> Path:
    """mod_panel_users (ADMIN-оверрайды ролей) хранится в mod.<profile>.db
    Cigilbot — собственной БД, физически отдельной от bot.<profile>.db в
    twitch-bots. Профиль "main" — дефолтный, без суффикса INSTANCE."""
    return ROOT / f"mod.{profile}.db" if profile != MAIN_PROFILE else ROOT / "mod.db"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Фоновая задача внутри процесса панели, не отдельный OS-процесс — см.
    # докстринг cigilbot/supervisor.py про компромисс этого решения.
    task = asyncio.create_task(supervisor_loop(str(ROOT / "registry.db")))
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(moderation_router)
app.include_router(registry_router)

# .env — единственный конфиг-файл панели в Cigilbot (в отличие от
# twitch-bots, где .env.moderation отличал этот процесс от panel/server.py
# на 8765 в той же папке, здесь других панелей на другом порту нет,
# делить нечего). _write_env_values (panel/auth.py) тоже пишет TWITCH_MOD_*
# сюда же после логина, и cigilbot/consumer.py читает PANEL_TWITCH_* из
# этого же файла — все три места обязаны совпадать, иначе токен бота
# будет записан не туда, откуда его потом читают.
ENV_FILENAME = ".env"
_panel_auth_config = load_panel_auth_config(ROOT, env_filename=ENV_FILENAME, default_port=8766)


def _read_own_env(key: str) -> str:
    env_file = ROOT / ENV_FILENAME
    if not env_file.exists():
        return ""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{key}="):
            return stripped.split("=", 1)[1]
    return ""


_session_secret = os.environ.get("PANEL_SESSION_SECRET", "") or _read_own_env("PANEL_SESSION_SECRET")
if not _session_secret:
    # Временный секрет, если вход ещё не настроен — auth_login всё равно
    # отдаст 503 без PANEL_TWITCH_* (см. PanelAuthConfig.configured),
    # роли/действия защищены require_role* независимо от секрета сессии.
    _session_secret = secrets.token_hex(32)

app.state.panel_auth_config = _panel_auth_config
app.state.panel_root = ROOT
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
    content = (Path(__file__).parent / "static" / filename).read_bytes()
    return hashlib.sha256(content).hexdigest()[:10]


_STATIC_VERSIONS = {name: _static_hash(name) for name in ("moderation.js",)}


@app.get("/")
@app.get("/moderation")
def moderation_page() -> HTMLResponse:
    html = (Path(__file__).parent / "static" / "moderation.html").read_text(encoding="utf-8")
    for filename, version in _STATIC_VERSIONS.items():
        html = html.replace(f'src="/static/{filename}"', f'src="/static/{filename}?v={version}"')
    return HTMLResponse(html)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8766)

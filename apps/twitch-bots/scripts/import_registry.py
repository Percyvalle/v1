#!/usr/bin/env python
"""CLI: одноразовый импорт существующих .env.<profile> ботов в registry.db.

Использование:
    .venv\\Scripts\\python scripts\\import_registry.py --dry-run
    .venv\\Scripts\\python scripts\\import_registry.py

Читает все .env.<profile> файлы в корне twitch-bots (тот же glob, что
panel/server.py::list_profiles — продублирован здесь напрямую, а не
импортирован из panel.server, чтобы не тянуть побочные эффекты импорта
FastAPI-приложения/роутеров того модуля в отдельный CLI-скрипт), резолвит
TWITCH_CHANNEL (login) каждого профиля в broadcaster_id через Helix App
Access Token (bot/twitch_helix.py), и пишет в registry.db.

Не трогает ничего живого: только читает .env-файлы и Helix API, пишет в
НОВЫЙ файл registry.db. Безопасно запускать повторно (upsert по
broadcaster_id) и держать live-каналы работающими во время прогона.

--dry-run печатает, что было бы сделано, ничего не пишет и не резолвит
через Helix.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.registry import ChannelRegistry  # noqa: E402
from bot.twitch_helix import HelixResolveError, HelixResolver  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Только напечатать найденные профили, ничего не писать и не резолвить через Helix",
    )
    parser.add_argument(
        "--registry-db", type=Path, default=ROOT / "registry.db",
        help="Путь к registry.db (по умолчанию twitch-bots/registry.db)",
    )
    parser.add_argument(
        "--only-profiles", type=str, default=None,
        help=(
            "Список профилей через запятую, ограничивающий импорт (например "
            "'cigilbot,paver_papa') — нужно, если несколько .env.<profile> "
            "указывают на один и тот же канал (например main/ari на общего "
            "бота Шинру, не участвующего в модерации): без фильтра upsert по "
            "broadcaster_id тихо оставит только последний обработанный профиль."
        ),
    )
    return parser.parse_args()


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def _find_profiles(root: Path) -> list[tuple[str, Path]]:
    """[(profile, env_file)] для каждого известного профиля, включая "main"
    (.env без суффикса) — тот же glob, что panel/server.py::list_profiles."""
    candidates: list[tuple[str, Path]] = []
    main_env = root / ".env"
    if main_env.exists():
        candidates.append(("main", main_env))
    for p in sorted(root.glob(".env.*")):
        if p.name == ".env.example":
            continue
        candidates.append((p.name.removeprefix(".env."), p))
    return candidates


async def main() -> int:
    args = parse_args()

    profiles = _find_profiles(ROOT)
    if not profiles:
        print("Профилей .env* не найдено — импортировать нечего.")
        return 0

    if args.only_profiles is not None:
        allowed = {p.strip() for p in args.only_profiles.split(",") if p.strip()}
        skipped = [profile for profile, _ in profiles if profile not in allowed]
        profiles = [(profile, env_file) for profile, env_file in profiles if profile in allowed]
        if skipped:
            print(f"Пропущены флагом --only-profiles: {skipped}")

    print(f"Найдено профилей: {len(profiles)}")
    entries: list[tuple[str, str, dict[str, str]]] = []  # (profile, login, values)
    for profile, env_file in profiles:
        values = _read_env_file(env_file)
        login = values.get("TWITCH_CHANNEL", "").strip().lstrip("#").lower()
        if not login:
            print(f"  {profile}: TWITCH_CHANNEL не задан, пропущен")
            continue
        entries.append((profile, login, values))
        print(f"  {profile} -> {login}")

    if not entries:
        print("Ни один профиль не содержит TWITCH_CHANNEL — импортировать нечего.")
        return 0

    if args.dry_run:
        print("\n--dry-run: Helix не вызывался, registry.db не изменён.")
        return 0

    root_values = _read_env_file(ROOT / ".env") if (ROOT / ".env").exists() else {}
    client_id = os.environ.get("PANEL_TWITCH_CLIENT_ID", "") or root_values.get("PANEL_TWITCH_CLIENT_ID", "")
    client_secret = (
        os.environ.get("PANEL_TWITCH_CLIENT_SECRET", "") or root_values.get("PANEL_TWITCH_CLIENT_SECRET", "")
    )
    if not client_id or not client_secret:
        print(
            "PANEL_TWITCH_CLIENT_ID/SECRET не заданы (ни в окружении, ни в .env) — "
            "не могу резолвить broadcaster_id через Helix.",
            file=sys.stderr,
        )
        return 1

    resolver = HelixResolver(client_id, client_secret)
    registry = ChannelRegistry(str(args.registry_db))
    await registry.connect()

    try:
        logins = [login for _, login, _ in entries]
        try:
            users = await resolver.resolve_logins(logins)
        except HelixResolveError as exc:
            print(f"Ошибка Helix при резолве логинов {logins}: {exc}", file=sys.stderr)
            return 1

        by_login = {u.login.lower(): u for u in users}
        missing = [login for login in logins if login not in by_login]
        if missing:
            print(f"Не найдены на Twitch (пропущены): {missing}", file=sys.stderr)

        for profile, login, values in entries:
            user = by_login.get(login)
            if user is None:
                continue
            voice_enabled = values.get("VOICE_ENABLED", "").strip().lower() == "true"
            moderation_enabled = values.get("MODERATION_ENABLED", "true").strip().lower() != "false"
            record = await registry.upsert_channel(
                broadcaster_id=user.id,
                login=user.login,
                display_name=user.display_name,
                voice_enabled=voice_enabled,
                moderation_enabled=moderation_enabled,
            )
            print(f"  записано: profile={profile} login={record.login} broadcaster_id={record.broadcaster_id}")

        print(f"\nИмпорт завершён: {args.registry_db}")
        return 0
    finally:
        await resolver.close()
        await registry.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

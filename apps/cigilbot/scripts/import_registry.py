#!/usr/bin/env python
"""CLI: одноразовый импорт существующих .env.<profile> каналов в registry.db.

Использование:
    .venv\\Scripts\\python scripts\\import_registry.py --dry-run
    .venv\\Scripts\\python scripts\\import_registry.py

Читает все .env.<profile> файлы в корне Cigilbot (тот же паттерн, что
panel/auth.py::_list_profile_channels — переиспользован буквально, не
импортирован из-за цикличного импорта panel->cigilbot, тот же trade-off,
что уже принят в проекте, см. panel/moderation_api.py:58-61), резолвит
TWITCH_CHANNEL (login) в broadcaster_id через Helix (App Access Token,
PANEL_TWITCH_CLIENT_ID/SECRET из корневого .env — тот же клиент, что
account_age_client в cigilbot/consumer.py), и пишет в registry.db.

Не трогает ничего живого: только читает .env-файлы и Helix API, пишет в
НОВЫЙ файл registry.db. Безопасно запускать повторно (upsert по
broadcaster_id) и держать live-каналы работающими во время прогона.

--dry-run печатает, что было бы сделано, ничего не пишет и не резолвит
через Helix (чтобы не тратить запросы впустую при проверке списка).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cigilbot.registry_store import RegistryStore  # noqa: E402
from cigilbot.twitch_api import HelixClient, HelixError  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Только напечатать найденные профили, ничего не писать и не резолвить через Helix",
    )
    parser.add_argument(
        "--registry-db", type=Path, default=ROOT / "registry.db",
        help="Путь к registry.db (по умолчанию Cigilbot/registry.db)",
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
    """[(profile, env_file)] для каждого известного .env.<profile> — тот же
    glob, что panel/auth.py::_list_profile_channels, без .env "main" (в
    Cigilbot корневой .env — это конфигурация панели/токенов, не профиль
    канала, в отличие от twitch-bots)."""
    candidates: list[tuple[str, Path]] = []
    for p in sorted(root.glob(".env.*")):
        if p.name.endswith(".example"):
            continue
        candidates.append((p.name.removeprefix(".env."), p))
    return candidates


async def main() -> int:
    args = parse_args()

    profiles = _find_profiles(ROOT)
    if not profiles:
        print("Профилей .env.<profile> не найдено — импортировать нечего.")
        return 0

    print(f"Найдено профилей: {len(profiles)}")
    entries: list[tuple[str, str]] = []  # (profile, login)
    for profile, env_file in profiles:
        values = _read_env_file(env_file)
        login = values.get("TWITCH_CHANNEL", "").strip().lstrip("#").lower()
        if not login:
            print(f"  {profile}: TWITCH_CHANNEL не задан, пропущен")
            continue
        entries.append((profile, login))
        print(f"  {profile} -> {login}")

    if not entries:
        print("Ни один профиль не содержит TWITCH_CHANNEL — импортировать нечего.")
        return 0

    if args.dry_run:
        print("\n--dry-run: Helix не вызывался, registry.db не изменён.")
        return 0

    client_id = os.environ.get("PANEL_TWITCH_CLIENT_ID", "")
    client_secret = os.environ.get("PANEL_TWITCH_CLIENT_SECRET", "")
    root_env = ROOT / ".env"
    if root_env.exists() and (not client_id or not client_secret):
        root_values = _read_env_file(root_env)
        client_id = client_id or root_values.get("PANEL_TWITCH_CLIENT_ID", "")
        client_secret = client_secret or root_values.get("PANEL_TWITCH_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        print(
            "PANEL_TWITCH_CLIENT_ID/SECRET не заданы (ни в окружении, ни в .env) — "
            "не могу резолвить broadcaster_id через Helix.",
            file=sys.stderr,
        )
        return 1

    helix = HelixClient(client_id, client_secret)
    registry = RegistryStore(str(args.registry_db))
    await registry.connect()

    try:
        logins = [login for _, login in entries]
        try:
            users = await helix.get_users(logins=logins)
        except HelixError as exc:
            print(f"Ошибка Helix при резолве логинов {logins}: {exc}", file=sys.stderr)
            return 1

        by_login = {u.login.lower(): u for u in users}
        missing = [login for login in logins if login not in by_login]
        if missing:
            print(f"Не найдены на Twitch (пропущены): {missing}", file=sys.stderr)

        for profile, login in entries:
            user = by_login.get(login)
            if user is None:
                continue
            record = await registry.upsert_channel(
                broadcaster_id=user.id,
                login=user.login,
                display_name=user.display_name,
                registered_by="manual",
            )
            print(f"  записано: profile={profile} login={record.login} broadcaster_id={record.broadcaster_id}")

        print(f"\nИмпорт завершён: {args.registry_db}")
        return 0
    finally:
        await helix.close()
        await registry.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

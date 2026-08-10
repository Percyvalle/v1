# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Language

Code comments, docstrings, log messages, user-facing strings, and commit messages in this
repository are written in **Russian**. Match that when editing — do not translate existing
Russian prose to English, and write new comments in Russian.

## Repository layout

Monorepo of two Twitch projects that share a git history and nothing else:

| | `apps/twitch-bots` | `apps/cigilbot` |
|---|---|---|
| Purpose | AI chat companion (DeepSeek) + voice input | Anti-spam moderation engine + panel |
| Panel port | 8765 | 8766 |
| Own `.venv`, own `.env`, own `pyproject.toml` | yes | yes |
| Tests | 30 (only `panel/auth.py`) | ~570 across 27 files |

**They are deliberately two separate processes with separate virtualenvs.** Do not merge the
environments, do not add a shared package, do not import across `apps/`. The reasons (a crash
in one must not take the other down; `twitch-bots` pulls ~600 MB of `faster-whisper`/`numpy`
that moderation has no use for) are documented in the root `README.md`.

Python 3.12 on Windows. Neither project is a package — `pyproject.toml` intentionally has no
`[build-system]`/`[project]` section, only ruff/pytest/mypy config.

## Commands

Every command runs from inside a project directory, against that project's own venv.
`.venv/` is gitignored, so on a fresh clone it must be created first.

```powershell
# Setup (per project)
cd apps\cigilbot            # or apps\twitch-bots
python -m venv .venv
.\.venv\Scripts\pip install -r requirements-dev.txt   # includes requirements.txt
copy .env.example .env

# Checks
.\.venv\Scripts\pytest
.\.venv\Scripts\pytest tests/test_engine.py                       # one file
.\.venv\Scripts\pytest tests/test_engine.py::test_name -x         # one test
.\.venv\Scripts\ruff check .
.\.venv\Scripts\mypy                                              # config-driven, no args
```

`pytest` is configured with `asyncio_mode = "auto"` — async tests need no decorator.
`filterwarnings` turns `DeprecationWarning` from own code into an error. A `slow` marker is
registered in both projects but currently unused.

Running the stack (three processes for a live stream):

```powershell
cd apps\cigilbot
.\.venv\Scripts\python -m panel.moderation_server        # port 8766; also runs the supervisor
.\.venv\Scripts\python -m cigilbot.consumer <broadcaster_id>   # one per channel

cd ..\twitch-bots
.\.venv\Scripts\python main.py                           # Twitch IRC
.\.venv\Scripts\python voice_main.py                     # only if VOICE_ENABLED=true
.\.venv\Scripts\python -m panel.server                   # port 8765
```

`consumer.py` takes a **numeric `broadcaster_id`**, not a channel name, and the channel must
already exist in `registry.db`. The supervisor inside the 8766 panel starts and stops
consumers on its own from each channel's `desired_state`, so manual launch is for debugging.

## Architecture

### How the two projects talk

Through a **file on disk**, with no HTTP in the hot path and no shared code:

```
apps/twitch-bots/main.py                  apps/cigilbot/cigilbot/consumer.py
  reads Twitch IRC                          owns the long-lived ModerationEngine
  writes every message   --mod_inbox-->     drains the queue, analyses, writes
  into bot.db                                verdicts into mod.<broadcaster_id>.db
  (never waits for a reply)
```

`mod_inbox` is a table inside `bot.db` — a file belonging to the **other** project.
`consumer.py` opens its own connection to that same file (WAL makes this safe) and polls it.
If Cigilbot is down, messages accumulate; nothing is lost and nothing blocks. The path is
`BOT_PROJECT_ROOT`, defaulting to `../twitch-bots`.

There is exactly **one** HTTP path between them: Channel Registry mirroring,
`twitch-bots/panel/server.py` → `POST /api/registry/channels` on 8766, authenticated by the
shared secret `INTERNAL_SYNC_TOKEN` in an `X-Internal-Token` header. If Cigilbot is
unreachable the channel is still created locally.

### Databases — five distinct file families, easy to confuse

| File | Owner | Contents |
|---|---|---|
| `twitch-bots/bot.db` | twitch-bots | viewers, chat history, `mod_inbox`, `panel_admins` |
| `twitch-bots/registry.db` | twitch-bots | Channel Registry — **source of truth** for which channels exist |
| `cigilbot/registry.db` | cigilbot | independent mirror of the above |
| `cigilbot/mod.db` | cigilbot | only `mod_panel_users` (ADMIN role overrides for the 8766 panel) |
| `cigilbot/mod.<broadcaster_id>.db` | cigilbot | all moderation state — **one file per channel**, because the engine is stateful |

`bot.db` may carry an `INSTANCE` suffix (`bot.<instance>.db`) under the legacy profile model
— see below. `bot/database.py` runs `executescript` on every connect with no versioning;
`registry.db` and `mod.*.db` use real migrations (`PRAGMA user_version` /
`cigilbot/migrations.py`).

### Channel identity

Channels are keyed by **`broadcaster_id`** (the stable numeric Twitch ID), never by `login`,
which changes when a channel is renamed. One bot account serves all channels at once —
`main.py::_load_initial_channels()` reads the active list from `registry.db` and passes it to
twitchio's `initial_channels`; `cfg.channel` from `.env` is only a fallback for an empty
registry.

The `profile` parameter throughout `panel/moderation_api.py` is **historical naming** — its
value is a `broadcaster_id`. It was left unrenamed so the frontend (`moderation.js`) did not
have to change.

### Moderation pipeline (`apps/cigilbot`)

`engine.py::observe(event) -> Verdict` orchestrates:

```
Normalizer -> Detectors -> Cluster Detection -> Risk Score -> Confidence -> Policy -> Audit
```

Structural rules that hold the design together:

- **`types.py` imports nothing capable of I/O** — no aiosqlite, httpx, or twitchio. A detector
  therefore *cannot* ban anyone; the detection/action split is enforced by the import graph,
  not by convention.
- **`policy.py` hardcodes the safety invariants as module constants**, deliberately not read
  from YAML, so they cannot be weakened by editing config — not even in `AGGRESSIVE`/`ATTACK`
  sensitivity:
  - `MIN_FAMILIES_FOR_BAN = 2` — no BAN without two independent signal families
  - no BAN on a provisional verdict (Helix has not returned account age yet) → downgraded to TIMEOUT
  - moderators/VIPs/the broadcaster and users marked safe never get more than OBSERVE
  - TIMEOUT and BAN each require their confidence floor
  - every downgrade is recorded in `Verdict.blocked_by`
- **`Signal` requires non-empty `evidence`** and rejects values outside `[0,1]` in
  `__post_init__` — an unexplainable verdict is not representable.
- **`SignalFamily`** exists so two detectors describing the same fact ("exact duplicate" and
  "near duplicate") do not count as independent confirmation.
- Detectors read the sliding window *before* the current message is added; clustering reads it
  *after*. `engine.observe` inserts into the window strictly between those two steps.
- Panel-driven state (Attack Mode, Giveaway Mode, Pattern Library, FP penalties) is **cached in
  the engine and refreshed by explicit `reload_*`/`sync_*` calls** from the consumer's poll
  loop, never re-read per message. The panel is a separate process writing to the same DB.
- Config loading (`config.py`) rejects unknown YAML keys with `ConfigError` at startup rather
  than silently ignoring a typo.

The system runs in **SHADOW mode**: verdicts are computed and persisted, but nothing is
executed in Twitch until a moderator token is obtained through the panel's Settings screen.
`executor.py` only drains `mod_action_queue`; it never decides anything.

### Panels

Two independent FastAPI apps that do **not** call each other over HTTP. Because port is part of
the browser origin, logging in on 8765 does not log you in on 8766 — this is an accepted cost
of the split, not a bug.

Roles are derived from Twitch on every login, not stored: broadcaster → `OWNER`, channel
moderator (Helix `GET /moderation/moderators`) → `MODERATOR`, anyone else → `VIEWER`. `ADMIN`
is the only manual override, and each panel keeps its own list (`panel_admins` in `bot.db`
vs `mod_panel_users` in `mod.db`). Every route is guarded by
`Depends(require_role_min(...))` — in `panel/server.py` the auth import sits mid-file, above
the first route, specifically so the dependency exists when the decorators are evaluated
(`SEC-001`; routes there previously had no authorization at all).

`panel/auth.py` exists in **both** projects as an independent copy — same logic, different
storage for ADMIN overrides. Changes to one usually need porting to the other by hand.

The 8766 panel also controls the 8765 project's `main.py` process directly via subprocess
(`cigilbot/bot_process_control.py`), because twitchio cannot join a new channel without a
reconnect. Auto-restart is deliberately not implemented there: one `main.py` serves every
channel, so restarting it would drop moderation on channels that are live right now.

### Two coexisting channel models in `apps/twitch-bots`

`main.py` uses the Registry model (one account, all channels). `panel/server.py` still
implements the older per-profile model in full: `list_profiles()` scans `.env.<profile>`
files, `new_profile_from_template()` creates them, and `_start()`/`db_path()` fan processes
and databases out by `INSTANCE`. Cigilbot dropped profiles in Phase 1; twitch-bots did not.
Both paths work — check which one a change actually affects before editing.

## Conventions

- **Docstrings and comments explain *why this and not the alternative***, usually naming the
  concrete incident that motivated it (`BUG-002`, `BUG-003`, `BUG-004`, `SEC-001`,
  `FALSE-BAN-001`, `FALSE-BAN-002`) and what the code used to do instead. Ruff per-file
  ignores in `pyproject.toml` carry the same justification. Keep this up — a change that
  reverses one of these decisions should update the comment that explains it.
- There are **zero** TODO/FIXME/HACK markers in the codebase.
- Line length 100, `E501` disabled (formatter's job), `B008` disabled (FastAPI `Depends`).
- Failures in moderation/audit paths are logged with `log.exception` and swallowed — losing a
  verdict record must never drop a chat message.
- `apps/cigilbot` is mypy-`strict` over `cigilbot/`, `tests/`, and four `panel/` files.
  `apps/twitch-bots` checks only `panel/auth.py` and `tests/panel` — the rest of the bot was
  written without annotations and is intentionally excluded.
- `.gitattributes` normalizes to LF in the repo, CRLF in the working tree.

## Git

- **Do not add `Co-Authored-By: Claude` (or any other co-author trailer) to commit messages.**
  This overrides the default Claude Code behaviour. No commit in this repository's history has
  one, and it should stay that way.
- Commit messages are Russian, subject line in the imperative or `topic: what changed`
  (`mypy: починить конфиг и типы, которые он не проверял`). Non-trivial commits carry a body
  of bullets explaining *why* each change was made, and close with a `Проверено:` line stating
  what was actually run and its result.

## Documentation

- `docs/moderation-plan.md` — moderation engine architecture, safety invariants, stages 0–10.
  The code references its sections by number ("раздел 23 ТЗ").
- `docs/master-plan.html` — roadmap, phases 1–9, direction 00 (Channel Registry).
- `docs/phase1-plan.html` — what Phase 1 built, its security review, and its incidents.

Phase 1 is closed and verified on live channels; Phase 2 (Alerts) is next.

## Known inconsistencies

Findings from a full read of the tree — worth fixing, and worth knowing about before trusting
a comment or a template:

1. `apps/twitch-bots/requirements.txt` is missing `streamlink` and `av` (PyAV). Both are
   imported at module level in `bot/audio_source.py`, and `streamlink` also in
   `panel/server.py`, so a clean install cannot start the 8765 panel or `voice_main.py`.
2. `apps/cigilbot/.env.example` has no `INTERNAL_SYNC_TOKEN`, although the project README
   requires it and `panel/registry_api.py` reads it (503 without it).
3. `apps/twitch-bots/.env.example` does not document `STREAMER_CONTEXT`, `VOICE_SOURCE`,
   `VOICE_STREAM_CHANNEL`, `VOICE_SILENCE_THRESHOLD`, `VOICE_FREE_REPLY_COOLDOWN`, `INSTANCE`,
   or `BOT_ENV_FILE`, all of which `bot/config.py` reads.
4. Stale pre-monorepo paths survive in comments and templates: `../TWITCH BOTS`,
   `../Cigilbot` (wrong case — breaks on case-sensitive filesystems),
   `Cigilbot/cigilbot/...`, `bot/moderation/detectors/language.py`, `mod.<profile>.db`,
   and a claim that the two projects live in "разные репо".

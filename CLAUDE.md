# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Language

Code comments, docstrings, log messages, user-facing strings, and commit messages in this
repository are written in **Russian**. Match that when editing — do not translate existing
Russian prose to English, and write new comments in Russian.

## Repository layout

Monorepo of three directories: two engines and the panel that drives both.

| | `apps/twitch-bots` | `apps/cigilbot` | `apps/panel` |
|---|---|---|---|
| Purpose | AI chat companion (DeepSeek) + voice input | Anti-spam moderation engine | Web panel for both, port 8766 |
| Own `pyproject.toml` | yes | yes | yes |
| Tests | none | 462 across 26 files | 132 across 3 files |

**The two engines are deliberately separate processes.** A crash in moderation must not take
down the bot that is talking in chat, and `mod_inbox` between them gives backpressure: if
moderation stalls, chat does not wait. Do not merge `main.py` and `consumer.py`.

**The panel is deliberately one process.** It used to be two apps on 8765 and 8766, and
because the port is part of the browser origin, that cost a second login for no benefit —
both screens would live and die with the same uvicorn anyway. `apps/panel` imports from both
`bot.*` and `cigilbot.*`; that cross-import is normal **for the panel only**. The engines
still must not import each other.

One `.venv`, one `.env`, both in the repo root — the panel cannot be assembled from two
separate environments. Voice dependencies (~600 MB) are optional, in `requirements-voice.txt`.

Python 3.12 on Windows. None of the three is a package — `pyproject.toml` intentionally has no
`[build-system]`/`[project]` section, only ruff/pytest/mypy config.

## Commands

One venv in the repo root, shared by everything. `.venv/` is gitignored, so on a fresh clone
it must be created first. Tool commands still run from inside a project directory, because
ruff/pytest/mypy read their config from the current directory.

```powershell
# Setup (once, from the repo root)
python -m venv .venv
.\.venv\Scripts\pip install -r requirements-dev.txt   # includes requirements.txt
copy .env.example .env
.\.venv\Scripts\pip install -r requirements-voice.txt # only if VOICE_ENABLED=true

# Checks — from inside apps\cigilbot or apps\panel (apps\twitch-bots has ruff only)
cd apps\cigilbot
..\..\.venv\Scripts\pytest
..\..\.venv\Scripts\pytest tests/test_engine.py                    # one file
..\..\.venv\Scripts\pytest tests/test_engine.py::test_name -x      # one test
..\..\.venv\Scripts\ruff check .
..\..\.venv\Scripts\mypy                                           # config-driven, no args
```

`pytest` is configured with `asyncio_mode = "auto"` — async tests need no decorator.
`filterwarnings` turns `DeprecationWarning` from own code into an error. A `slow` marker is
registered but currently unused. The suites take ~10–30 s to run but the process lingers for
about a minute afterwards before exiting; this predates the panel merge and is not a hang.

`apps/twitch-bots` has no `[tool.mypy]` or `[tool.pytest.ini_options]` section at all — its
only tests checked `panel/auth.py` and moved to `apps/panel` with the panel. Running `mypy`
there errors with "Missing target module"; that is expected, not a broken config.

Running the stack:

```powershell
cd apps\panel
..\..\.venv\Scripts\python -m panel.server               # port 8766, both screens
```

That is normally the whole thing: the supervisor inside the panel starts and stops consumers
from each channel's `desired_state`, and the panel can start `main.py` itself. Manual launch
is for debugging only:

```powershell
cd apps\twitch-bots
..\..\.venv\Scripts\python main.py                       # Twitch IRC
..\..\.venv\Scripts\python voice_main.py                 # only if VOICE_ENABLED=true

cd ..\cigilbot
..\..\.venv\Scripts\python -m cigilbot.consumer <broadcaster_id>   # one per channel
```

`consumer.py` takes a **numeric `broadcaster_id`**, not a channel name, and the channel must
already exist in `registry.db`.

## Architecture

### How the two engines talk

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

There is now **no** HTTP path between them. Channel Registry mirroring used to be
`twitch-bots/panel/server.py` → `POST /api/registry/channels` on 8766 with the shared secret
`INTERNAL_SYNC_TOKEN`; both ends are the same process since the panel merge, so
`panel/bots_api.py::api_add_channel` writes the mirror directly and the "neighbour is
unreachable" failure mode is gone. The endpoint still exists in `panel/registry_api.py`,
still token-guarded, as an entry point for an external caller — nothing internal uses it.

### Databases — five distinct file families, easy to confuse

| File | Owner | Contents |
|---|---|---|
| `var/twitch-bots/bot.db` | twitch-bots | viewers, chat history, `mod_inbox` |
| `var/twitch-bots/registry.db` | twitch-bots | Channel Registry — **source of truth** for which channels exist |
| `var/cigilbot/registry.db` | cigilbot | independent mirror of the above |
| `var/cigilbot/mod.db` | cigilbot | only `mod_panel_users` — ADMIN role overrides for **both** panel screens |
| `var/cigilbot/mod.<broadcaster_id>.db` | cigilbot | all moderation state — **one file per channel**, because the engine is stateful |

`panel_admins` in `bot.db` is gone: with one panel there is one list of ADMINs, and it lives
in `mod_panel_users`. `bot/database.py` no longer creates the table, but does not drop it
either — `apps/panel/scripts/merge_panel_admins.py` is the one-off that moves existing rows
across (higher role wins on conflict, never downgrades).

### `var/` — all runtime state, outside the source trees

```
var/twitch-bots/   bot.db, registry.db, usage.json, voice_input.txt, logs/, run/, panel_state/
var/cigilbot/      registry.db, mod.db, mod.<broadcaster_id>.db, logs/, run/
```

The whole directory is one line in `.gitignore`, replacing a list of masks (`*.db`, `*.pid`,
`logs/`, `usage*.json`, …) that had to grow with every new kind of working file, where a miss
meant a live database or a secret in a commit.

Three modules define these paths and **must agree**: `bot/paths.py`, `cigilbot/paths.py` and
`panel/paths.py`. The panel duplicates the definitions rather than importing them, because
its own `sys.path` bootstrap (`panel/__init__.py`) imports `panel.paths` and cannot depend on
the engines being importable yet. `test_merged_panel.py::test_panel_and_engines_agree_on_state_dirs`
asserts the duplicate has not drifted — if it does, the panel writes one file while the engine
reads another.

Paths are absolute. `bot/config.py` used to return `"bot.db"` relative to the current
directory, which worked only because the panel always launched `main.py` with
`cwd=apps/twitch-bots`; from anywhere else the same code silently created a fresh empty
database instead of opening the existing one.

`ensure_dirs()` is called before anything opens a file under `var/` — on a fresh clone the
directory does not exist at all. Note it must run **before** taking a pid-lock, not inside it:
the lock file itself lives in `var/*/run/`.

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

### The panel (`apps/panel`)

One FastAPI app on 8766 with two screens: `/moderation` (default, also `/`) and `/bots`. It
lives outside both engines and imports from both, which is why `panel/__init__.py` puts
`apps/twitch-bots` and `apps/cigilbot` on `sys.path` — that bootstrap sits in the package
`__init__`, not in `server.py`, so tests importing routers directly get it too.

Because the panel is no longer inside either project, `Path(__file__).parent.parent` stopped
meaning "the project root". `panel/paths.py` names the three roots explicitly, and
`PanelRoots` carries them through `app.state.panel_roots`:

- `repo` — the single `.env`. **`_write_env_values` writes `TWITCH_MOD_*` here and
  `cigilbot/consumer.py` reads them from here.** If these two paths ever diverge, the panel
  will report a token was obtained while every ban fails with 401.
- `bot` — `apps/twitch-bots`: `.env.<profile>`, `bot.db`, `main.py`, `prompts/`.
- `cigilbot` — `apps/cigilbot`: `registry.db`, `mod.db`, `mod.<broadcaster_id>.db`.

Roles are derived from Twitch on every login, not stored: broadcaster → `OWNER`, channel
moderator (Helix `GET /moderation/moderators`) → `MODERATOR`, anyone else → `VIEWER`. `ADMIN`
is the only manual override, one list for both screens (`mod_panel_users`). Every route is
guarded by `Depends(require_role_min(...))` — in `panel/bots_api.py` the auth import sits
above the first route specifically so the dependency exists when decorators are evaluated
(`SEC-001`; routes there previously had no authorization at all).

`panel/auth.py` used to exist as an independent copy in each project, and they had drifted —
the same `httpx.ConnectTimeout` bug in `_resolve_roles_by_channel` was fixed twice. One copy
now. `_list_profile_channels` returns the **union** of both channel models, because
`role_for_profile` receives a `broadcaster_id` from `moderation_api` and a profile name from
the bots screen; Registry wins on key collision.

The panel controls `main.py` directly via subprocess (`cigilbot/bot_process_control.py`),
because twitchio cannot join a new channel without a reconnect. Auto-restart is deliberately
not implemented there: one `main.py` serves every channel, so restarting it would drop
moderation on channels that are live right now.

### Two coexisting channel models

`main.py` uses the Registry model (one account, all channels). `panel/bots_api.py` still
implements the older per-profile model in full: `list_profiles()` scans `.env.<profile>`
files, `new_profile_from_template()` creates them, and `_start()`/`db_path()` fan processes
and databases out by `INSTANCE`. Cigilbot dropped profiles in Phase 1; twitch-bots did not,
and the panel merge deliberately did not change that — the job was one login and one port,
not redoing how bots are created. Both paths work — check which one a change actually affects
before editing. Note that profile `main` now reads the **repo-root** `.env`, while named
profiles stay in `apps/twitch-bots/.env.<profile>`.

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
- `apps/cigilbot` is mypy-`strict` over `cigilbot/` and `tests/`. `apps/panel` is `strict` over
  everything except `panel/bots_api.py`. `apps/twitch-bots` is not type-checked at all — the
  bot was written without annotations and is intentionally excluded, and `bot.*` is pulled in
  under `follow_imports = "skip"` so the panel's `mypy_path` does not drag it into scope.
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

1. `twitch-bots/registry.db` and `cigilbot/registry.db` are two mirrors of one list, now
   written by the same process one after the other — the HTTP hop that justified the split
   is gone. Collapsing them into one is a data-model change (it touches
   `main.py::_load_initial_channels`, `consumer.py`, the supervisor and both registry
   routers), not a layout one, which is why it was left alone.
2. `apps/twitch-bots/scripts/import_registry.py` is a one-off from the Registry migration.
   It still works; the Cigilbot copy of it was deleted because that project dropped
   `.env.<profile>` in Phase 1, so it could only ever print "импортировать нечего".
3. The engines' own `docs/*.html` still describe the two-panel split as current.

Earlier entries here are fixed and gone: missing `streamlink`/`av` (now pinned in the root
`requirements.txt`/`requirements-voice.txt`), undocumented `.env` keys (the root
`.env.example` documents every key both engines read, `INTERNAL_SYNC_TOKEN` included),
stale pre-monorepo paths in comments (`bot/moderation/...`, `mod.<profile>.db`,
`../TWITCH BOTS`), the dead `apps/cigilbot/.venv`, and runtime state living inside the
source trees — all state now lives in `var/`, see below.

# AGENTS.md

## What this repo is

An Open WebUI "tool", not a standalone app: a single Python file pasted into Open WebUI's Tools editor, running inside the Open WebUI server (`src/common/base.py` imports `open_webui.*` internals).

This AGENTS.md is shared with the sibling Open WebUI tool repos (e.g. `openwebui-email-tools`, `openwebui-calendar-tools`, `openwebui-nas-tools`): they use the same `src/` layout, the same `build.py` bundler, and the same conventions. Only the tool domain (email, calendar, NAS, ...) differs.

- Source of truth: `src/` (entry point `src/main.py` defines the `Tools` class).
- The generated distribution file `openwebui_<domain>_tools.py` is committed. Never edit it by hand — rebuild after changing `src/`:

  ```
  .venv/bin/python build.py --output openwebui_<domain>_tools.py
  ```

  (Also the default VS Code build task, which already contains the correct output name.) The build ends with a `compile()` syntax check; a successful build is the main verification available here.

## Shared layout

- `src/main.py` — tool entry point: metadata docstring, `Valves`/`UserValves`, public tool methods, protocol handler dispatch.
- `src/common/` — shared across tools: `base.py` (`CustomTool` mixin), `decorators.py` (`@custom_tool`, `@with_context`), `exceptions.py`.
- `src/clients/` — one client per protocol (e.g. `mail_client.py`, `calendar_client.py`, `synology_client.py`), plus the shared `terminal_client.py` (Open Terminal integration).
- `src/common/` modules and `terminal_client.py` are duplicated across the sibling repos and must be kept in sync when changed.

## Bundler constraints (`build.py` hard-fails on violations)

The bundler removes local imports and moves module-level globals into a single block at the top of the generated file, so `src/` code must follow these rules:

- Local imports: `from src.module import Name` only, at top level (not inside functions/classes). No `import src.x`, no `as` aliases, no wildcard imports.
- Statements the bundler removes (local imports, the module docstring) must be alone on their line.
- No circular imports; all local modules must live under `src/`.
- Duplicate module-level globals across files are deduplicated with a printed warning — `log = logging.getLogger(__name__)` in every module is expected — but two different declarations of the same name is an error.

## Tool metadata

The generated file's header (`title`, `author`, `git_url`, `description`, `license`, `version`, `required_open_webui_version`, `requirements`) comes from the module docstring in `src/main.py`. Edit that docstring — not the generated file — to bump the version or deps. `requirements` names the tool's third-party dependency (e.g. `imapclient`, `caldav`, `requests, paramiko, smbprotocol`); keep it in sync with the README.

## Runtime context (why you can't run this locally)

The tool executes *inside* Open WebUI and imports its internals (`open_webui.models.*`, `open_webui.routers.*`, vector DB retrieval, storage), so the code cannot be run or tested outside Open WebUI. Importing the tool locally is impossible: the repo `.venv` contains only pip and exists just to run `build.py` (Python 3.12). Unresolved imports of `open_webui`, `fastapi`, and the tool's third-party requirement in an editor are normal.

## Verification

No tests, lint, typecheck, or CI exist in these repos. Verification = a successful build (it compiles the output) plus Ruff formatting (VS Code format-on-save; no ruff config file, so defaults apply).

## Tool anatomy

- `@custom_tool` on the `Tools` class mixes in `CustomTool` (`src/common/base.py`).
- Public tool methods (e.g. `search_email_messages`, `search_calendar_events`) are decorated with `@with_context`, which opens the protocol session, converts exceptions to `{"error": ...}` JSON, and manages per-call context.
- Dunder params (`__request__`, `__user__`, `__metadata__`, `__event_emitter__`, `__event_call__`) are injected by Open WebUI at call time — not regular arguments.
- `Valves` / `UserValves` are pydantic models declared on the `Tools` class; protocol handler dispatch lives in `_get_handlers`.

## Style

- Match existing code: double quotes, magic trailing commas, `_`-prefixed private methods.

## Compatibility

The tools import Open WebUI internals, so releases of Open WebUI can break them (tested 0.10.2–0.13.3). Keep `required_open_webui_version` in `src/main.py` accurate when touching internals imports.

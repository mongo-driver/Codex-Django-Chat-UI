# Codex Django Control Panel

This project runs Codex CLI from a Django web app using local browser-based login credentials.

It uses a project-local auth store:
- `./.codex/auth.json`
- `CODEX_HOME=./.codex` for all Codex subprocess calls

No `sk-` key is required for ChatGPT login mode.

## Features

- Django-based web UI (replaces previous Flask app)
- Browser login via `codex login`
- Login status check via `codex login status`
- Prompt execution via `codex exec`
- Local auth state viewer (`auth_mode`, account id, token preview, resolved Codex CLI path)
- Model chooser from local Codex model metadata (with custom model override)
- Reasoning controls (`model_reasoning_effort`, `model_reasoning_summary`, `model_verbosity`)
- Execution insights panel with:
  - token usage (from JSON events when available)
  - selected model context window and estimated remaining context
  - rate-limit fields when exposed by Codex CLI output
- Advanced option controls for Codex:
  - `exec`: `-c`, `--enable`, `--disable`, `--image`, `-m`, `--oss`, `--local-provider`, `--sandbox`, `--profile`, `--full-auto`, `--dangerously-bypass-approvals-and-sandbox`, `--cd`, `--skip-git-repo-check`, `--add-dir`, `--output-schema`, `--color`, `--json`, `-o`
  - `login`: `-c`, `--enable`, `--disable`, `--device-auth`, `--with-api-key`
- "Extra args" inputs for login/exec to pass any additional/new Codex options.
- CLI help viewer (`codex --help`, `codex exec --help`, `codex login --help`)

## Requirements

- Windows (this repo currently targets your local setup)
- Codex CLI installed and working in terminal
- Python virtual environment: `env1`

## Run (always using `env1`)

1. Activate env:
   - PowerShell: `.\env1\Scripts\Activate.ps1`
2. Install deps:
   - `python -m pip install -r requirements.txt`
3. Migrate DB:
   - `python manage.py migrate`
4. Start server:
   - `python manage.py runserver`
5. Open:
   - `http://127.0.0.1:8000`

## Notes

- If Codex CLI is not found:
  - Set env var `CODEX_CLI_PATH` to your Codex executable path.
  - Typical global npm path on Windows:
    - `C:\Users\<you>\AppData\Roaming\npm\codex.cmd`
- Secrets are not committed:
  - `.codex/` is ignored in `.gitignore`.

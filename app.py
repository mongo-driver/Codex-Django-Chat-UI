import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from flask import Flask, render_template, request

app = Flask(__name__)
DEFAULT_MODEL = "gpt-5.3-codex"
BASE_DIR = Path(__file__).resolve().parent
LOCAL_CODEX_HOME = BASE_DIR / ".codex"
LOCAL_AUTH_FILE = LOCAL_CODEX_HOME / "auth.json"


@app.route("/", methods=["GET", "POST"])
def index():
    response_text = None
    error_message = None
    info_message = None
    model = DEFAULT_MODEL

    if request.method == "POST":
        action = (request.form.get("action") or "").strip().lower()
        model = (request.form.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL

        if action == "login":
            ok, message = codex_login()
            if ok:
                info_message = message
            else:
                error_message = message
        elif action == "chat":
            chat_input = (request.form.get("chat_input") or "").strip()
            if not chat_input:
                error_message = "Please enter a message."
            else:
                response_text, error_message = chat_with_codex(chat_input, model)
        else:
            error_message = "Invalid action."

    auth_state = load_auth_state()

    return render_template(
        "index.html",
        response_text=response_text,
        error_message=error_message,
        info_message=info_message,
        model=model,
        default_model=DEFAULT_MODEL,
        auth_state=auth_state,
    )


def codex_login():
    codex_exe = resolve_codex_executable()
    if codex_exe is None:
        return False, codex_not_found_message()

    try:
        proc = subprocess.run(
            [codex_exe, "login"],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            env=codex_env(),
        )
    except FileNotFoundError:
        return False, "Codex CLI not found. Install it and ensure `codex` is on PATH."
    except subprocess.TimeoutExpired:
        return False, "Login timed out. Please try again and finish browser login faster."

    combined = "\n".join(part.strip() for part in [proc.stdout or "", proc.stderr or ""] if part.strip())
    if proc.returncode != 0:
        return False, f"Codex login failed.\n{combined or 'No error output.'}"

    return True, "Login command finished. Browser auth should now be connected to Codex."


def chat_with_codex(message, model):
    output_file = None
    codex_exe = resolve_codex_executable()
    if codex_exe is None:
        return None, codex_not_found_message()

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp:
            output_file = tmp.name

        cmd = [
            codex_exe,
            "exec",
            "--skip-git-repo-check",
            "--color",
            "never",
            "-m",
            model,
            "-o",
            output_file,
            message,
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
            env=codex_env(),
        )
    except FileNotFoundError:
        return None, "Codex CLI not found. Install it and ensure `codex` is on PATH."
    except subprocess.TimeoutExpired:
        return None, "Codex request timed out."
    except OSError as exc:
        return None, f"Failed to execute Codex CLI: {exc}"
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        details = stderr or stdout or "No error output."
        return None, f"Codex exec failed.\n{details}"

    try:
        if output_file and Path(output_file).exists():
            text = Path(output_file).read_text(encoding="utf-8").strip()
            if text:
                return text, None
    except OSError:
        pass
    finally:
        if output_file:
            try:
                Path(output_file).unlink(missing_ok=True)
            except OSError:
                pass

    return None, "Codex returned no final message."


def load_auth_state():
    state = {
        "logged_in": False,
        "auth_mode": None,
        "account_id": None,
        "token_preview": None,
        "error": None,
        "auth_path": str(LOCAL_AUTH_FILE),
        "codex_cli_path": resolve_codex_executable() or "not found",
    }
    if not LOCAL_AUTH_FILE.exists():
        return state

    try:
        data = json.loads(LOCAL_AUTH_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        state["error"] = f"Failed to read auth file: {exc}"
        return state

    tokens = data.get("tokens") or {}
    access_token = (tokens.get("access_token") or "").strip()
    state["auth_mode"] = data.get("auth_mode")
    state["account_id"] = tokens.get("account_id")

    if access_token:
        state["logged_in"] = True
        state["token_preview"] = mask_token(access_token)

    return state


def mask_token(token):
    if len(token) <= 18:
        return token
    return f"{token[:12]}...{token[-6:]}"


def codex_env():
    LOCAL_CODEX_HOME.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["CODEX_HOME"] = str(LOCAL_CODEX_HOME)
    path_items = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    npm_dir = str(Path.home() / "AppData" / "Roaming" / "npm")
    if npm_dir not in path_items:
        path_items.insert(0, npm_dir)
        env["PATH"] = os.pathsep.join(path_items)
    return env


def resolve_codex_executable():
    env_override = (os.getenv("CODEX_CLI_PATH") or "").strip()
    if env_override and Path(env_override).exists():
        return env_override

    for name in ["codex.cmd", "codex", "codex.ps1"]:
        found = shutil.which(name)
        if found:
            return found

    candidates = [
        Path.home() / "AppData" / "Roaming" / "npm" / "codex.cmd",
        Path.home() / "AppData" / "Roaming" / "npm" / "codex.ps1",
        BASE_DIR / "node_modules" / ".bin" / "codex.cmd",
        BASE_DIR / "node_modules" / ".bin" / "codex",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def codex_not_found_message():
    return (
        "Codex CLI not found.\n"
        "Set CODEX_CLI_PATH to your codex executable or install globally with npm.\n"
        "Common path on Windows: C:\\Users\\<you>\\AppData\\Roaming\\npm\\codex.cmd"
    )


if __name__ == "__main__":
    app.run(debug=True)

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.shortcuts import render

DEFAULT_MODEL = "gpt-5.3-codex"
DEFAULT_EXEC_TIMEOUT_SECONDS = 600
DEFAULT_LOGIN_TIMEOUT_SECONDS = 300


def index(request):
    form = build_form_state(request)
    response_text = None
    error_message = None
    info_message = None
    command_output = None
    cli_help = None

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip().lower()
        if action == "exec":
            ok, response_text, error_message, command_output = run_exec(form)
            if ok and not response_text:
                info_message = "Command completed but no final message file content was returned."
        elif action == "login":
            ok, info_message, error_message, command_output = run_login(form)
        elif action == "status":
            ok, info_message, error_message, command_output = run_login_status(form)
        elif action == "help":
            cli_help = fetch_codex_help(form)
            if cli_help["error"]:
                error_message = cli_help["error"]
            else:
                info_message = "Codex CLI help refreshed."
        else:
            error_message = "Unknown action."

    auth_state = load_auth_state()
    if cli_help is None:
        cli_help = {"root": "", "exec": "", "login": "", "error": None}

    context = {
        "form": form,
        "response_text": response_text,
        "error_message": error_message,
        "info_message": info_message,
        "command_output": command_output,
        "auth_state": auth_state,
        "cli_help": cli_help,
    }
    return render(request, "codexui/index.html", context)


def run_exec(form):
    codex_exe = resolve_codex_executable()
    if not codex_exe:
        return False, None, codex_not_found_message(), None

    prompt = form["prompt"].strip()
    if not prompt:
        return False, None, "Message is required for `codex exec`.", None

    timeout_seconds, timeout_error = parse_timeout(
        form["exec_timeout_seconds"], DEFAULT_EXEC_TIMEOUT_SECONDS
    )
    if timeout_error:
        return False, None, timeout_error, None

    extra_args, extra_error = parse_extra_args(form["extra_exec_args"])
    if extra_error:
        return False, None, extra_error, None

    output_file = form["output_last_message_file"].strip()
    output_is_temp = False
    if not output_file:
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        temp.close()
        output_file = temp.name
        output_is_temp = True

    cmd = [codex_exe, "exec"]
    cmd.extend(build_common_options(form["exec_config_overrides"], form["exec_enable"], form["exec_disable"]))
    cmd.extend(add_repeat_option("--image", split_lines(form["images"])))
    if form["model"].strip():
        cmd.extend(["-m", form["model"].strip()])
    if form["oss"]:
        cmd.append("--oss")
    if form["local_provider"]:
        cmd.extend(["--local-provider", form["local_provider"]])
    if form["sandbox_mode"]:
        cmd.extend(["--sandbox", form["sandbox_mode"]])
    if form["profile"].strip():
        cmd.extend(["--profile", form["profile"].strip()])
    if form["full_auto"]:
        cmd.append("--full-auto")
    if form["dangerous_bypass"]:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    if form["cd_dir"].strip():
        cmd.extend(["--cd", form["cd_dir"].strip()])
    if form["skip_git_repo_check"]:
        cmd.append("--skip-git-repo-check")
    cmd.extend(add_repeat_option("--add-dir", split_lines(form["add_dirs"])))
    if form["output_schema"].strip():
        cmd.extend(["--output-schema", form["output_schema"].strip()])
    if form["color"]:
        cmd.extend(["--color", form["color"]])
    if form["json_output"]:
        cmd.append("--json")
    cmd.extend(["-o", output_file])
    cmd.extend(extra_args)
    cmd.append(prompt)

    result = run_codex_command(cmd, timeout_seconds=timeout_seconds)
    if result["error"]:
        return False, None, result["error"], result["output"]
    if result["returncode"] != 0:
        return False, None, f"Codex exec failed.\n{result['output'] or 'No output'}", result["output"]

    try:
        response_text = Path(output_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        return False, None, f"Command succeeded but output file read failed: {exc}", result["output"]
    finally:
        if output_is_temp:
            try:
                Path(output_file).unlink(missing_ok=True)
            except OSError:
                pass

    return True, response_text, None, result["output"]


def run_login(form):
    codex_exe = resolve_codex_executable()
    if not codex_exe:
        return False, None, codex_not_found_message(), None

    timeout_seconds, timeout_error = parse_timeout(
        form["login_timeout_seconds"], DEFAULT_LOGIN_TIMEOUT_SECONDS
    )
    if timeout_error:
        return False, None, timeout_error, None

    extra_args, extra_error = parse_extra_args(form["extra_login_args"])
    if extra_error:
        return False, None, extra_error, None

    cmd = [codex_exe, "login"]
    cmd.extend(build_common_options(form["login_config_overrides"], form["login_enable"], form["login_disable"]))
    if form["device_auth"]:
        cmd.append("--device-auth")
    stdin_text = None
    if form["login_api_key"].strip():
        cmd.append("--with-api-key")
        stdin_text = form["login_api_key"].strip() + "\n"
    cmd.extend(extra_args)

    result = run_codex_command(cmd, timeout_seconds=timeout_seconds, stdin_text=stdin_text)
    if result["error"]:
        return False, None, result["error"], result["output"]
    if result["returncode"] != 0:
        return False, None, f"Codex login failed.\n{result['output'] or 'No output'}", result["output"]
    return True, "Login command finished.", None, result["output"]


def run_login_status(form):
    codex_exe = resolve_codex_executable()
    if not codex_exe:
        return False, None, codex_not_found_message(), None

    timeout_seconds, timeout_error = parse_timeout(
        form["login_timeout_seconds"], DEFAULT_LOGIN_TIMEOUT_SECONDS
    )
    if timeout_error:
        return False, None, timeout_error, None

    result = run_codex_command([codex_exe, "login", "status"], timeout_seconds=timeout_seconds)
    if result["error"]:
        return False, None, result["error"], result["output"]
    if result["returncode"] != 0:
        return False, None, f"Codex login status failed.\n{result['output'] or 'No output'}", result["output"]
    return True, result["output"] or "No status output.", None, result["output"]


def fetch_codex_help(form):
    codex_exe = resolve_codex_executable()
    if not codex_exe:
        return {"root": "", "exec": "", "login": "", "error": codex_not_found_message()}

    timeout_seconds, timeout_error = parse_timeout(
        form["help_timeout_seconds"], DEFAULT_LOGIN_TIMEOUT_SECONDS
    )
    if timeout_error:
        return {"root": "", "exec": "", "login": "", "error": timeout_error}

    root = run_codex_command([codex_exe, "--help"], timeout_seconds=timeout_seconds)
    exec_help = run_codex_command([codex_exe, "exec", "--help"], timeout_seconds=timeout_seconds)
    login_help = run_codex_command([codex_exe, "login", "--help"], timeout_seconds=timeout_seconds)
    error = None
    if root["error"] or exec_help["error"] or login_help["error"]:
        error = root["error"] or exec_help["error"] or login_help["error"]

    return {
        "root": root["output"],
        "exec": exec_help["output"],
        "login": login_help["output"],
        "error": error,
    }


def run_codex_command(cmd, timeout_seconds, stdin_text=None):
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env=codex_env(),
        )
    except FileNotFoundError:
        return {"returncode": -1, "output": "", "error": codex_not_found_message()}
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "output": "", "error": f"Command timed out after {timeout_seconds} seconds."}
    except OSError as exc:
        return {"returncode": -1, "output": "", "error": f"Failed to execute command: {exc}"}

    output = "\n".join(part for part in [(proc.stdout or "").strip(), (proc.stderr or "").strip()] if part)
    return {"returncode": proc.returncode, "output": output, "error": None}


def build_form_state(request):
    post = request.POST if request.method == "POST" else None
    return {
        "model": get_value(post, "model", DEFAULT_MODEL),
        "prompt": get_value(post, "prompt", ""),
        "exec_timeout_seconds": get_value(post, "exec_timeout_seconds", str(DEFAULT_EXEC_TIMEOUT_SECONDS)),
        "exec_config_overrides": get_value(post, "exec_config_overrides", ""),
        "exec_enable": get_value(post, "exec_enable", ""),
        "exec_disable": get_value(post, "exec_disable", ""),
        "images": get_value(post, "images", ""),
        "oss": get_bool(post, "oss", False),
        "local_provider": get_value(post, "local_provider", ""),
        "sandbox_mode": get_value(post, "sandbox_mode", ""),
        "profile": get_value(post, "profile", ""),
        "full_auto": get_bool(post, "full_auto", False),
        "dangerous_bypass": get_bool(post, "dangerous_bypass", False),
        "cd_dir": get_value(post, "cd_dir", ""),
        "skip_git_repo_check": get_bool(post, "skip_git_repo_check", True),
        "add_dirs": get_value(post, "add_dirs", ""),
        "output_schema": get_value(post, "output_schema", ""),
        "color": get_value(post, "color", "auto"),
        "json_output": get_bool(post, "json_output", False),
        "output_last_message_file": get_value(post, "output_last_message_file", ""),
        "extra_exec_args": get_value(post, "extra_exec_args", ""),
        "login_timeout_seconds": get_value(post, "login_timeout_seconds", str(DEFAULT_LOGIN_TIMEOUT_SECONDS)),
        "login_config_overrides": get_value(post, "login_config_overrides", ""),
        "login_enable": get_value(post, "login_enable", ""),
        "login_disable": get_value(post, "login_disable", ""),
        "device_auth": get_bool(post, "device_auth", False),
        "login_api_key": get_value(post, "login_api_key", ""),
        "extra_login_args": get_value(post, "extra_login_args", ""),
        "help_timeout_seconds": get_value(post, "help_timeout_seconds", str(DEFAULT_LOGIN_TIMEOUT_SECONDS)),
    }


def get_value(post, key, default):
    if post is None:
        return default
    return post.get(key, default)


def get_bool(post, key, default=False):
    if post is None:
        return default
    value = post.get(key)
    if value is None:
        return False
    return value.lower() in {"1", "true", "on", "yes"}


def parse_timeout(value, default_value):
    raw = (value or "").strip()
    if not raw:
        return default_value, None
    try:
        timeout = int(raw)
    except ValueError:
        return None, f"Timeout value must be an integer: {raw!r}"
    if timeout < 1:
        return None, "Timeout must be >= 1 second."
    return timeout, None


def parse_extra_args(text):
    raw = (text or "").strip()
    if not raw:
        return [], None
    try:
        return shlex.split(raw, posix=False), None
    except ValueError as exc:
        return None, f"Invalid extra args: {exc}"


def split_lines(text):
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def split_csv(text):
    entries = []
    for part in (text or "").replace("\n", ",").split(","):
        item = part.strip()
        if item:
            entries.append(item)
    return entries


def add_repeat_option(option_name, values):
    args = []
    for value in values:
        args.extend([option_name, value])
    return args


def build_common_options(config_text, enable_text, disable_text):
    args = []
    args.extend(add_repeat_option("-c", split_lines(config_text)))
    args.extend(add_repeat_option("--enable", split_csv(enable_text)))
    args.extend(add_repeat_option("--disable", split_csv(disable_text)))
    return args


def load_auth_state():
    auth_file = local_auth_file()
    state = {
        "logged_in": False,
        "auth_mode": None,
        "account_id": None,
        "token_preview": None,
        "error": None,
        "auth_path": str(auth_file),
        "codex_cli_path": resolve_codex_executable() or "not found",
        "codex_home": str(local_codex_home()),
    }
    if not auth_file.exists():
        return state

    try:
        data = json.loads(auth_file.read_text(encoding="utf-8"))
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


def local_codex_home():
    return Path(settings.BASE_DIR) / ".codex"


def local_auth_file():
    return local_codex_home() / "auth.json"


def codex_env():
    codex_home = local_codex_home()
    codex_home.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)

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

    base_dir = Path(settings.BASE_DIR)
    candidates = [
        Path.home() / "AppData" / "Roaming" / "npm" / "codex.cmd",
        Path.home() / "AppData" / "Roaming" / "npm" / "codex.ps1",
        base_dir / "node_modules" / ".bin" / "codex.cmd",
        base_dir / "node_modules" / ".bin" / "codex",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def codex_not_found_message():
    return (
        "Codex CLI not found.\n"
        "Set CODEX_CLI_PATH to your codex executable or install globally with npm.\n"
        "Common Windows path: C:\\Users\\<you>\\AppData\\Roaming\\npm\\codex.cmd"
    )

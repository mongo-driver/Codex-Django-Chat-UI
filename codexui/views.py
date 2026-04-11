import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.shortcuts import render

DEFAULT_MODEL = "gpt-5.3-codex"
DEFAULT_EXEC_TIMEOUT_SECONDS = 600
DEFAULT_LOGIN_TIMEOUT_SECONDS = 300
DEFAULT_REASONING_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh"]
SESSION_CHATS_KEY = "codexui_chats"
SESSION_ACTIVE_CHAT_KEY = "codexui_active_chat"
SESSION_THEME_KEY = "codexui_theme"
MAX_CHAT_MESSAGES = 60


def index(request):
    chats, active_chat = ensure_chat_state(request)
    theme = get_theme(request)
    post = request.POST if request.method == "POST" else None
    action = (post.get("action") or "").strip().lower() if post else ""

    info_message = None
    response_text = None
    error_message = None
    command_output = None
    cli_help = {"root": "", "exec": "", "login": "", "error": None}
    exec_metrics = None

    if request.method == "POST":
        if action == "set_theme":
            theme_value = (post.get("theme") or "").strip().lower()
            if theme_value in {"dark", "light"}:
                set_theme(request, theme_value)
                theme = get_theme(request)
        elif action == "new_chat":
            new_title = (post.get("new_chat_title") or "").strip()
            new_chat = new_chat_state(new_title, len(chats) + 1)
            chats.insert(0, new_chat)
            save_chat_state(request, chats, new_chat["id"])
            chats, active_chat = ensure_chat_state(request)
            info_message = f"Created {new_chat['title']}."
        elif action == "switch_chat":
            target_chat_id = (post.get("chat_id") or "").strip()
            if set_active_chat(request, chats, target_chat_id):
                chats, active_chat = ensure_chat_state(request)
            else:
                error_message = "Chat not found."
        elif action == "delete_chat":
            target_chat_id = (post.get("chat_id") or active_chat["id"]).strip()
            if len(chats) <= 1:
                error_message = "At least one chat must remain."
            else:
                next_chats = [item for item in chats if item["id"] != target_chat_id]
                if not next_chats:
                    error_message = "Unable to delete the last chat."
                else:
                    save_chat_state(request, next_chats, next_chats[0]["id"])
                    chats, active_chat = ensure_chat_state(request)
                    info_message = "Chat deleted."
        elif action == "clear_chat":
            active_chat["thread_id"] = ""
            active_chat["messages"] = []
            active_chat["last_exec_metrics"] = None
            active_chat["settings"] = default_form_state()
            save_chat_state(request, chats, active_chat["id"])
            info_message = "Active chat context was cleared."

    chats, active_chat = ensure_chat_state(request)
    seeded_form = (
        active_chat.get("settings")
        if isinstance(active_chat.get("settings"), dict)
        else default_form_state()
    )
    form = build_form_state(
        post if action in {"exec", "login", "status", "help"} else None, seeded_form
    )

    model_catalog = get_model_catalog()
    selected_model = get_effective_model(form)
    selected_model_info = find_model_info(model_catalog, selected_model)
    reasoning_effort_options = get_reasoning_effort_options(selected_model_info)

    if request.method == "POST":
        if action == "exec":
            ok, response_text, error_message, command_output, exec_metrics = run_exec(
                form, model_catalog, active_chat
            )
            active_chat["settings"] = form
            active_chat["last_exec_metrics"] = exec_metrics
            if ok:
                append_chat_message(active_chat, "user", form["prompt"])
                if response_text:
                    append_chat_message(active_chat, "assistant", response_text)
            save_chat_state(request, chats, active_chat["id"])
            if ok and not response_text:
                info_message = (
                    "Command completed but no final message file content was returned."
                )
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
        elif action not in {
            "set_theme",
            "new_chat",
            "switch_chat",
            "delete_chat",
            "clear_chat",
        }:
            error_message = "Unknown action."

    chats, active_chat = ensure_chat_state(request)
    if exec_metrics is None and isinstance(active_chat.get("last_exec_metrics"), dict):
        exec_metrics = active_chat["last_exec_metrics"]

    auth_state = load_auth_state()
    show_advanced = bool(error_message) or action in {"login", "status", "help"} or (
        action == "exec" and form_has_advanced_overrides(form)
    )

    context = {
        "form": form,
        "response_text": response_text,
        "error_message": error_message,
        "info_message": info_message,
        "command_output": command_output,
        "auth_state": auth_state,
        "cli_help": cli_help,
        "model_catalog": model_catalog,
        "selected_model": selected_model,
        "selected_model_info": selected_model_info,
        "reasoning_effort_options": reasoning_effort_options,
        "exec_metrics": exec_metrics,
        "theme": theme,
        "theme_toggle_target": "light" if theme == "dark" else "dark",
        "theme_toggle_label": "Disable dark theme" if theme == "dark" else "Enable dark theme",
        "chats": chats,
        "active_chat": active_chat,
        "show_advanced": show_advanced,
    }
    return render(request, "codexui/index.html", context)


def run_exec(form, model_catalog, active_chat):
    codex_exe = resolve_codex_executable()
    if not codex_exe:
        return False, None, codex_not_found_message(), None, None

    prompt = form["prompt"].strip()
    if not prompt:
        return False, None, "Message is required for `codex exec`.", None, None

    timeout_seconds, timeout_error = parse_timeout(
        form["exec_timeout_seconds"], DEFAULT_EXEC_TIMEOUT_SECONDS
    )
    if timeout_error:
        return False, None, timeout_error, None, None

    extra_args, extra_error = parse_extra_args(form["extra_exec_args"])
    if extra_error:
        return False, None, extra_error, None, None

    output_file = form["output_last_message_file"].strip()
    output_is_temp = False
    if not output_file:
        temp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        temp.close()
        output_file = temp.name
        output_is_temp = True

    selected_model = get_effective_model(form)
    resume_thread_id = (active_chat.get("thread_id") or "").strip() if active_chat else ""

    cmd = [codex_exe, "exec"]
    if resume_thread_id:
        cmd.extend(["resume", resume_thread_id])
    cmd.extend(
        build_common_options(
            form["exec_config_overrides"], form["exec_enable"], form["exec_disable"]
        )
    )
    cmd.extend(build_reasoning_options(form))
    cmd.extend(add_repeat_option("--image", split_lines(form["images"])))
    if selected_model:
        cmd.extend(["-m", selected_model])
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
    detected_thread_id = (
        extract_thread_id(result.get("stdout", ""), result.get("output", ""))
        or resume_thread_id
    )
    exec_metrics = build_exec_metrics(
        result,
        selected_model,
        model_catalog,
        form,
        detected_thread_id,
        bool(resume_thread_id),
    )

    if result["error"]:
        return False, None, result["error"], result["output"], exec_metrics
    if result["returncode"] != 0:
        return (
            False,
            None,
            f"Codex exec failed.\n{result['output'] or 'No output'}",
            result["output"],
            exec_metrics,
        )

    try:
        response_text = Path(output_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        return (
            False,
            None,
            f"Command succeeded but output file read failed: {exc}",
            result["output"],
            exec_metrics,
        )
    finally:
        if output_is_temp:
            try:
                Path(output_file).unlink(missing_ok=True)
            except OSError:
                pass

    if active_chat is not None:
        active_chat["thread_id"] = detected_thread_id or ""

    return True, response_text, None, result["output"], exec_metrics


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
    cmd.extend(
        build_common_options(
            form["login_config_overrides"], form["login_enable"], form["login_disable"]
        )
    )
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
        return (
            False,
            None,
            f"Codex login failed.\n{result['output'] or 'No output'}",
            result["output"],
        )
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

    result = run_codex_command(
        [codex_exe, "login", "status"], timeout_seconds=timeout_seconds
    )
    if result["error"]:
        return False, None, result["error"], result["output"]
    if result["returncode"] != 0:
        return (
            False,
            None,
            f"Codex login status failed.\n{result['output'] or 'No output'}",
            result["output"],
        )
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
    exec_help = run_codex_command(
        [codex_exe, "exec", "--help"], timeout_seconds=timeout_seconds
    )
    login_help = run_codex_command(
        [codex_exe, "login", "--help"], timeout_seconds=timeout_seconds
    )
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
        return {
            "returncode": -1,
            "output": "",
            "stdout": "",
            "stderr": "",
            "error": codex_not_found_message(),
        }
    except subprocess.TimeoutExpired:
        return {
            "returncode": -1,
            "output": "",
            "stdout": "",
            "stderr": "",
            "error": f"Command timed out after {timeout_seconds} seconds.",
        }
    except OSError as exc:
        return {
            "returncode": -1,
            "output": "",
            "stdout": "",
            "stderr": "",
            "error": f"Failed to execute command: {exc}",
        }

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    output = "\n".join(part for part in [stdout.strip(), stderr.strip()] if part)
    return {
        "returncode": proc.returncode,
        "output": output,
        "stdout": stdout,
        "stderr": stderr,
        "error": None,
    }


def default_form_state(seed=None):
    defaults = {
        "model_select": DEFAULT_MODEL,
        "model": "",
        "prompt": "",
        "reasoning_effort": "",
        "reasoning_summary": "",
        "model_verbosity": "",
        "exec_timeout_seconds": str(DEFAULT_EXEC_TIMEOUT_SECONDS),
        "exec_config_overrides": "",
        "exec_enable": "",
        "exec_disable": "",
        "images": "",
        "oss": False,
        "local_provider": "",
        "sandbox_mode": "",
        "profile": "",
        "full_auto": False,
        "dangerous_bypass": False,
        "cd_dir": "",
        "skip_git_repo_check": True,
        "add_dirs": "",
        "output_schema": "",
        "color": "auto",
        "json_output": True,
        "output_last_message_file": "",
        "extra_exec_args": "",
        "login_timeout_seconds": str(DEFAULT_LOGIN_TIMEOUT_SECONDS),
        "login_config_overrides": "",
        "login_enable": "",
        "login_disable": "",
        "device_auth": False,
        "login_api_key": "",
        "extra_login_args": "",
        "help_timeout_seconds": str(DEFAULT_LOGIN_TIMEOUT_SECONDS),
    }
    if isinstance(seed, dict):
        for key, value in seed.items():
            if key in defaults and value is not None:
                defaults[key] = value
    return defaults


def build_form_state(post=None, seed=None):
    defaults = default_form_state(seed)
    return {
        "model_select": get_value(post, "model_select", defaults["model_select"]),
        "model": get_value(post, "model", defaults["model"]),
        "prompt": get_value(post, "prompt", defaults["prompt"]),
        "reasoning_effort": get_value(post, "reasoning_effort", defaults["reasoning_effort"]),
        "reasoning_summary": get_value(post, "reasoning_summary", defaults["reasoning_summary"]),
        "model_verbosity": get_value(post, "model_verbosity", defaults["model_verbosity"]),
        "exec_timeout_seconds": get_value(
            post, "exec_timeout_seconds", defaults["exec_timeout_seconds"]
        ),
        "exec_config_overrides": get_value(
            post, "exec_config_overrides", defaults["exec_config_overrides"]
        ),
        "exec_enable": get_value(post, "exec_enable", defaults["exec_enable"]),
        "exec_disable": get_value(post, "exec_disable", defaults["exec_disable"]),
        "images": get_value(post, "images", defaults["images"]),
        "oss": get_bool(post, "oss", defaults["oss"]),
        "local_provider": get_value(post, "local_provider", defaults["local_provider"]),
        "sandbox_mode": get_value(post, "sandbox_mode", defaults["sandbox_mode"]),
        "profile": get_value(post, "profile", defaults["profile"]),
        "full_auto": get_bool(post, "full_auto", defaults["full_auto"]),
        "dangerous_bypass": get_bool(
            post, "dangerous_bypass", defaults["dangerous_bypass"]
        ),
        "cd_dir": get_value(post, "cd_dir", defaults["cd_dir"]),
        "skip_git_repo_check": get_bool(
            post, "skip_git_repo_check", defaults["skip_git_repo_check"]
        ),
        "add_dirs": get_value(post, "add_dirs", defaults["add_dirs"]),
        "output_schema": get_value(post, "output_schema", defaults["output_schema"]),
        "color": get_value(post, "color", defaults["color"]),
        "json_output": get_bool(post, "json_output", defaults["json_output"]),
        "output_last_message_file": get_value(
            post, "output_last_message_file", defaults["output_last_message_file"]
        ),
        "extra_exec_args": get_value(post, "extra_exec_args", defaults["extra_exec_args"]),
        "login_timeout_seconds": get_value(
            post, "login_timeout_seconds", defaults["login_timeout_seconds"]
        ),
        "login_config_overrides": get_value(
            post, "login_config_overrides", defaults["login_config_overrides"]
        ),
        "login_enable": get_value(post, "login_enable", defaults["login_enable"]),
        "login_disable": get_value(post, "login_disable", defaults["login_disable"]),
        "device_auth": get_bool(post, "device_auth", defaults["device_auth"]),
        "login_api_key": get_value(post, "login_api_key", defaults["login_api_key"]),
        "extra_login_args": get_value(post, "extra_login_args", defaults["extra_login_args"]),
        "help_timeout_seconds": get_value(
            post, "help_timeout_seconds", defaults["help_timeout_seconds"]
        ),
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
        return default
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


def build_reasoning_options(form):
    args = []
    if form["reasoning_effort"]:
        args.extend(["-c", f"model_reasoning_effort={toml_string(form['reasoning_effort'])}"])
    if form["reasoning_summary"]:
        args.extend(
            ["-c", f"model_reasoning_summary={toml_string(form['reasoning_summary'])}"]
        )
    if form["model_verbosity"]:
        args.extend(["-c", f"model_verbosity={toml_string(form['model_verbosity'])}"])
    return args


def toml_string(value):
    return json.dumps(str(value))


def get_effective_model(form):
    custom = (form.get("model") or "").strip()
    if custom:
        return custom
    return (form.get("model_select") or "").strip() or DEFAULT_MODEL


def extract_thread_id(stdout_text, combined_output):
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        if item.get("type") == "thread.started":
            thread_id = (item.get("thread_id") or "").strip()
            if thread_id:
                return thread_id

    match = re.search(r'"thread_id"\s*:\s*"([^"]+)"', combined_output)
    if match:
        return match.group(1).strip()
    return ""


def build_exec_metrics(
    result, selected_model, model_catalog, form, thread_id=None, resumed=False
):
    usage, usage_source, event_types = extract_usage_from_output(
        result.get("stdout", ""), result.get("output", "")
    )
    rate_limits = extract_rate_limits(result.get("stdout", ""), result.get("output", ""))
    model_info = find_model_info(model_catalog, selected_model)

    context_window = model_info.get("context_window") if model_info else None
    total_tokens = None
    if usage:
        total_tokens = usage.get("total_tokens")
        if total_tokens is None:
            input_tokens = usage.get("input_tokens") or 0
            output_tokens = usage.get("output_tokens") or 0
            try:
                total_tokens = int(input_tokens) + int(output_tokens)
            except (TypeError, ValueError):
                total_tokens = None

    context_remaining = None
    if context_window is not None and total_tokens is not None:
        context_remaining = max(int(context_window) - int(total_tokens), 0)

    return {
        "model": selected_model,
        "thread_id": thread_id or "",
        "resumed": resumed,
        "context_window": context_window,
        "context_remaining": context_remaining,
        "usage": usage,
        "usage_source": usage_source,
        "usage_pretty": pretty_json(usage),
        "rate_limits": rate_limits,
        "rate_limits_pretty": pretty_json(rate_limits),
        "reasoning_effort": form["reasoning_effort"] or "default",
        "reasoning_summary": form["reasoning_summary"] or "default",
        "model_verbosity": form["model_verbosity"] or "default",
        "event_types": event_types,
    }


def extract_usage_from_output(stdout_text, combined_output):
    usage = None
    event_types = []
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        event_type = item.get("type")
        if event_type:
            event_types.append(str(event_type))
        if event_type == "turn.completed" and isinstance(item.get("usage"), dict):
            usage = item.get("usage")

    if usage:
        return usage, "json", event_types

    match = re.search(r"tokens used\s*[\r\n]+([0-9][0-9,]*)", combined_output, re.IGNORECASE)
    if match:
        total_tokens = int(match.group(1).replace(",", ""))
        return {"total_tokens": total_tokens}, "text", event_types
    return None, "unavailable", event_types


def extract_rate_limits(stdout_text, combined_output):
    rate_limits = {}
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        collect_rate_limit_fields(item, rate_limits)

    header_keys = [
        "x-ratelimit-limit-requests",
        "x-ratelimit-remaining-requests",
        "x-ratelimit-reset-requests",
        "x-ratelimit-limit-tokens",
        "x-ratelimit-remaining-tokens",
        "x-ratelimit-reset-tokens",
    ]
    for key in header_keys:
        pattern = rf"(?im)^\s*{re.escape(key)}\s*[:=]\s*(.+)$"
        match = re.search(pattern, combined_output)
        if match:
            rate_limits[key] = match.group(1).strip()

    return rate_limits


def collect_rate_limit_fields(value, out):
    if isinstance(value, dict):
        for key, item in value.items():
            lower_key = str(key).lower()
            if lower_key.startswith("x-ratelimit") or (
                "rate" in lower_key and ("limit" in lower_key or "remaining" in lower_key)
            ):
                out[str(key)] = item
            collect_rate_limit_fields(item, out)
    elif isinstance(value, list):
        for item in value:
            collect_rate_limit_fields(item, out)


def pretty_json(value):
    if value is None:
        return ""
    try:
        return json.dumps(value, indent=2, ensure_ascii=False)
    except TypeError:
        return str(value)


@lru_cache(maxsize=1)
def get_model_catalog():
    candidates = [
        local_codex_home() / "models_cache.json",
        local_codex_home() / "models.json",
        Path(settings.BASE_DIR) / "_tmp_openai_codex_src" / "codex-rs" / "core" / "models.json",
    ]
    raw_models = []
    for path in candidates:
        models = load_models_file(path)
        if models:
            raw_models = models
            break
    if not raw_models:
        return fallback_model_catalog()

    catalog = []
    for model in raw_models:
        if not isinstance(model, dict):
            continue
        slug = (model.get("slug") or "").strip()
        if not slug:
            continue
        visibility = model.get("visibility")
        if visibility and str(visibility).lower() == "hidden":
            continue

        supported = []
        for level in model.get("supported_reasoning_levels") or []:
            if isinstance(level, dict) and level.get("effort"):
                supported.append(str(level["effort"]))
            elif isinstance(level, str):
                supported.append(level)
        catalog.append(
            {
                "slug": slug,
                "display_name": model.get("display_name") or slug,
                "description": model.get("description") or "",
                "context_window": model.get("context_window"),
                "default_reasoning_level": model.get("default_reasoning_level") or "",
                "supported_reasoning_levels": supported,
                "priority": model.get("priority", 9999),
            }
        )

    if not catalog:
        return fallback_model_catalog()

    catalog.sort(key=lambda item: (item["priority"], str(item["display_name"]).lower()))
    return catalog


def load_models_file(path):
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    if isinstance(data, dict):
        models = data.get("models")
        if isinstance(models, list):
            return models
    if isinstance(data, list):
        return data
    return []


def fallback_model_catalog():
    return [
        {
            "slug": "gpt-5.4",
            "display_name": "gpt-5.4",
            "description": "Latest frontier agentic coding model.",
            "context_window": 272000,
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": DEFAULT_REASONING_EFFORTS,
            "priority": 1,
        },
        {
            "slug": "gpt-5.4-mini",
            "display_name": "GPT-5.4-Mini",
            "description": "Smaller frontier agentic coding model.",
            "context_window": 272000,
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": DEFAULT_REASONING_EFFORTS,
            "priority": 2,
        },
        {
            "slug": "gpt-5.3-codex",
            "display_name": "gpt-5.3-codex",
            "description": "Codex optimized model.",
            "context_window": 272000,
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": DEFAULT_REASONING_EFFORTS,
            "priority": 3,
        },
        {
            "slug": "gpt-5.2",
            "display_name": "gpt-5.2",
            "description": "Balanced professional model.",
            "context_window": 272000,
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": DEFAULT_REASONING_EFFORTS,
            "priority": 4,
        },
    ]


def find_model_info(model_catalog, model_slug):
    for item in model_catalog:
        if item["slug"] == model_slug:
            return item
    return None


def get_reasoning_effort_options(model_info):
    if model_info and model_info.get("supported_reasoning_levels"):
        return model_info["supported_reasoning_levels"]
    return DEFAULT_REASONING_EFFORTS


def form_has_advanced_overrides(form):
    defaults = default_form_state()
    tracked_keys = [
        "model_select",
        "model",
        "reasoning_effort",
        "reasoning_summary",
        "model_verbosity",
        "exec_timeout_seconds",
        "exec_config_overrides",
        "exec_enable",
        "exec_disable",
        "images",
        "oss",
        "local_provider",
        "sandbox_mode",
        "profile",
        "full_auto",
        "dangerous_bypass",
        "cd_dir",
        "skip_git_repo_check",
        "add_dirs",
        "output_schema",
        "color",
        "json_output",
        "output_last_message_file",
        "extra_exec_args",
    ]
    for key in tracked_keys:
        if form.get(key) != defaults.get(key):
            return True
    return False


def get_theme(request):
    value = str(request.session.get(SESSION_THEME_KEY) or "dark").strip().lower()
    if value not in {"dark", "light"}:
        return "dark"
    return value


def set_theme(request, theme):
    request.session[SESSION_THEME_KEY] = theme
    request.session.modified = True


def new_chat_state(title, index_hint):
    resolved_title = (title or "").strip()
    if not resolved_title:
        resolved_title = f"Chat {index_hint}"
    return {
        "id": uuid.uuid4().hex[:12],
        "title": resolved_title[:80],
        "thread_id": "",
        "messages": [],
        "settings": default_form_state(),
        "last_exec_metrics": None,
    }


def sanitize_chat(raw_chat, fallback_title):
    if not isinstance(raw_chat, dict):
        raw_chat = {}

    chat_id = str(raw_chat.get("id") or uuid.uuid4().hex[:12]).strip()
    if not chat_id:
        chat_id = uuid.uuid4().hex[:12]

    title = str(raw_chat.get("title") or fallback_title).strip()[:80]
    if not title:
        title = fallback_title

    thread_id = str(raw_chat.get("thread_id") or "").strip()
    settings = build_form_state(None, raw_chat.get("settings"))

    messages = []
    for item in raw_chat.get("messages") or []:
        if not isinstance(item, dict):
            continue
        role = "assistant" if str(item.get("role")).lower() == "assistant" else "user"
        text = str(item.get("text") or "").strip()
        if text:
            messages.append({"role": role, "text": text})
    if len(messages) > MAX_CHAT_MESSAGES:
        messages = messages[-MAX_CHAT_MESSAGES:]

    last_exec_metrics = raw_chat.get("last_exec_metrics")
    if not isinstance(last_exec_metrics, dict):
        last_exec_metrics = None

    return {
        "id": chat_id,
        "title": title,
        "thread_id": thread_id,
        "messages": messages,
        "settings": settings,
        "last_exec_metrics": last_exec_metrics,
    }


def ensure_chat_state(request):
    raw_chats = request.session.get(SESSION_CHATS_KEY)
    chats = []
    if isinstance(raw_chats, list):
        for idx, item in enumerate(raw_chats, start=1):
            chats.append(sanitize_chat(item, f"Chat {idx}"))

    changed = False
    if not chats:
        chats = [new_chat_state("", 1)]
        changed = True

    active_chat_id = str(request.session.get(SESSION_ACTIVE_CHAT_KEY) or "").strip()
    active_chat = next((item for item in chats if item["id"] == active_chat_id), None)
    if active_chat is None:
        active_chat = chats[0]
        active_chat_id = active_chat["id"]
        changed = True

    if changed:
        save_chat_state(request, chats, active_chat_id)

    return chats, active_chat


def save_chat_state(request, chats, active_chat_id):
    request.session[SESSION_CHATS_KEY] = chats
    request.session[SESSION_ACTIVE_CHAT_KEY] = active_chat_id
    request.session.modified = True


def set_active_chat(request, chats, chat_id):
    for item in chats:
        if item["id"] == chat_id:
            save_chat_state(request, chats, item["id"])
            return True
    return False


def append_chat_message(chat, role, text):
    clean_text = (text or "").strip()
    if not clean_text:
        return
    if role not in {"user", "assistant"}:
        role = "assistant"
    chat["messages"].append({"role": role, "text": clean_text})
    if len(chat["messages"]) > MAX_CHAT_MESSAGES:
        chat["messages"] = chat["messages"][-MAX_CHAT_MESSAGES:]


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

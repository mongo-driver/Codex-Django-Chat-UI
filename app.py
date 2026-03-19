import os

import requests
from flask import Flask, render_template, request

app = Flask(__name__)
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-5.4-mini"


@app.route("/", methods=["GET", "POST"])
def index():
    response_text = None
    error_message = None

    if request.method == "POST":
        api_key = (request.form.get("api_key") or os.getenv("OPENAI_API_KEY", "")).strip()
        model = (request.form.get("model") or DEFAULT_MODEL).strip()
        chat_input = (request.form.get("chat_input") or "").strip()

        if not api_key:
            error_message = "API key is required (form field or OPENAI_API_KEY environment variable)."
        elif not chat_input:
            error_message = "Please enter a message."
        else:
            response_text, error_message = chat_with_openai(api_key, model, chat_input)

    return render_template(
        "index.html",
        response_text=response_text,
        error_message=error_message,
        default_model=DEFAULT_MODEL,
    )


def chat_with_openai(api_key, model, message):
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "input": message,
    }

    try:
        resp = requests.post(OPENAI_RESPONSES_URL, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        return None, f"Request failed: {exc}"

    output_text = data.get("output_text")
    if output_text:
        return output_text, None

    output = data.get("output", [])
    for item in output:
        if item.get("type") == "message":
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    text = content.get("text", "")
                    if text:
                        return text, None

    return None, "No text output returned by model."


if __name__ == "__main__":
    app.run(debug=True)

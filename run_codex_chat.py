#!/usr/bin/env python3
import argparse
import os
import sys

from django.core.management import execute_from_command_line


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run Codex Django Chat UI server.",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("CODEXWEB_HOST", "127.0.0.1"),
        help="Host to bind (default: 127.0.0.1 or CODEXWEB_HOST).",
    )
    parser.add_argument(
        "--port",
        default=os.getenv("CODEXWEB_PORT", "8000"),
        help="Port to bind (default: 8000 or CODEXWEB_PORT).",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "codexweb.settings")
    execute_from_command_line([sys.argv[0], "runserver", f"{args.host}:{args.port}"])


if __name__ == "__main__":
    main()

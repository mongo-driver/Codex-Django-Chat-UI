#!/usr/bin/env python3
import argparse
import os
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def parse_args():
    parser = argparse.ArgumentParser(
        description="Package built binary into a release zip.",
    )
    parser.add_argument("--version", required=True, help="Release version/tag.")
    parser.add_argument(
        "--name",
        default="codex-django-chat-ui",
        help="Binary base name (default: codex-django-chat-ui).",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    binary_name = args.name + (".exe" if os.name == "nt" else "")
    binary_path = Path("dist") / binary_name
    if not binary_path.exists():
        raise FileNotFoundError(f"Built binary not found: {binary_path}")

    runner_os = os.getenv("RUNNER_OS", "unknown").lower()
    runner_arch = os.getenv("RUNNER_ARCH", "unknown").lower()
    output_path = (
        Path("dist")
        / f"{args.name}-{args.version}-{runner_os}-{runner_arch}.zip"
    )

    with ZipFile(output_path, "w", ZIP_DEFLATED) as archive:
        archive.write(binary_path, arcname=binary_path.name)

    print(output_path.as_posix())


if __name__ == "__main__":
    main()

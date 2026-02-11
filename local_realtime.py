#!/usr/bin/env python3
"""Launcher for local realtime transcription from sibling project.

This wrapper runs `Dictation-Local-SenseVoice/dictation_realtime.py`
using that project's uv environment, so local model dependencies stay there.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def resolve_local_project(explicit: str | None) -> Path:
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if (candidate / "dictation_realtime.py").exists():
            return candidate
        raise SystemExit(f"ERROR: dictation_realtime.py not found under {candidate}")

    base = Path(__file__).resolve().parent.parent
    candidate = base / "Dictation-Local-SenseVoice"
    if (candidate / "dictation_realtime.py").exists():
        return candidate

    raise SystemExit(
        "ERROR: Could not find sibling project 'Dictation-Local-SenseVoice'. "
        "Use --local-project /path/to/Dictation-Local-SenseVoice"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run local realtime transcriber from sibling project",
        add_help=False,
    )
    parser.add_argument(
        "--wrapper-help",
        action="store_true",
        help="Show wrapper-specific help",
    )
    parser.add_argument(
        "--local-project",
        default=None,
        help="Path to Dictation-Local-SenseVoice project",
    )

    args, passthrough = parser.parse_known_args()
    if args.wrapper_help:
        parser.print_help()
        raise SystemExit(0)

    project = resolve_local_project(args.local_project)

    if "--display" not in passthrough:
        passthrough.extend(["--display", "text"])

    cmd = [
        "uv",
        "run",
        "--project",
        str(project),
        "python",
        str(project / "dictation_realtime.py"),
        *passthrough,
    ]

    env = dict(os.environ)
    env.pop("VIRTUAL_ENV", None)
    result = subprocess.run(cmd, env=env)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()

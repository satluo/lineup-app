"""Export and publish the public World Cup snapshot when its content changes."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "scripts" / "export_world_cup_snapshot.py"
SNAPSHOT = ROOT / "data" / "world-cup.json"
DEFAULT_SOURCE = ROOT.parent / "video-kb"


def run(*args: str) -> None:
    subprocess.run(args, cwd=ROOT, check=True)


def public_payload(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("generated_at", None)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    args = parser.parse_args()

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    if status.stdout.strip():
        raise RuntimeError("lineup-app worktree is not clean; public snapshot publish skipped")

    run("git", "pull", "--ff-only", "origin", "main")
    previous_bytes = SNAPSHOT.read_bytes() if SNAPSHOT.exists() else None
    previous_payload = public_payload(SNAPSHOT) if SNAPSHOT.exists() else None
    run(sys.executable, str(EXPORTER), "--source", str(args.source.resolve()))
    current_payload = public_payload(SNAPSHOT)

    if previous_payload == current_payload:
        if previous_bytes is not None:
            SNAPSHOT.write_bytes(previous_bytes)
        print("Public World Cup snapshot unchanged; no Pages publish needed.")
        return

    run("git", "add", "--", "data/world-cup.json", "assets/flags")
    staged = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=ROOT,
        check=False,
    )
    if staged.returncode == 0:
        print("Public World Cup snapshot has no staged content changes.")
        return
    if staged.returncode != 1:
        raise RuntimeError("unable to inspect staged World Cup snapshot changes")

    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    run("git", "commit", "-m", f"Sync World Cup snapshot {stamp}")
    run("git", "push", "origin", "main")
    print("Public World Cup snapshot pushed to GitHub Pages.")


if __name__ == "__main__":
    main()
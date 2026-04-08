"""
oz_macos.py — macOS統合の薄いラッパー (Sprint 1)

OZの個人エージェントが Mac を実際に操作するための土台。
すべての操作は capability + approval gate を通って oz_runtime から
だけ呼ばれることを前提にする。

セキュリティ:
- 起動可能アプリは /Applications/ または ~/Applications/ にバンドルが
  存在するもののみ。任意の AppleScript 実行はできない。
- アプリ名はパス・特殊文字をフィルタしてからクォート。
- AppleScript はリテラル文字列として渡し、エスケープを徹底。
- subprocess は shell=False、引数配列で実行 (シェル注入の経路なし)。
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Optional


# ================================
# Constants
# ================================
APPLICATIONS_DIRS = [
    "/Applications",
    "/System/Applications",
    os.path.expanduser("~/Applications"),
]

# Allowed app name characters: letters, digits, space, dot, dash, underscore,
# Japanese characters, emoji. Reject anything else to keep AppleScript safe.
_ALLOWED_NAME_CHARS = re.compile(
    r"^[\w\s\.\-\_\(\)\!\&\+\#"
    r"\u3040-\u309F"  # hiragana
    r"\u30A0-\u30FF"  # katakana
    r"\u4E00-\u9FFF"  # CJK
    r"\uFF00-\uFFEF"  # full-width forms
    r"]+$"
)


# ================================
# Helpers
# ================================
def _safe_app_name(name: str) -> Optional[str]:
    """Return the validated app name, or None if it should be rejected."""
    if not name:
        return None
    name = name.strip()
    if len(name) > 64:
        return None
    # Strip a trailing ".app" to be lenient with input
    if name.endswith(".app"):
        name = name[:-4]
    if not _ALLOWED_NAME_CHARS.match(name):
        return None
    return name


def _app_exists(name: str) -> bool:
    """Check that the bundle actually exists in a known Applications dir."""
    for d in APPLICATIONS_DIRS:
        if not os.path.isdir(d):
            continue
        candidate = os.path.join(d, f"{name}.app")
        if os.path.isdir(candidate):
            return True
    return False


def _quote_applescript_string(s: str) -> str:
    """Escape a string for safe inclusion in AppleScript double-quoted literals."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _run_osascript(script: str, timeout: float = 10.0) -> tuple[bool, str]:
    """Run a single AppleScript snippet via osascript and capture stdout."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "osascript timed out"
    except FileNotFoundError:
        return False, "osascript not found"

    if result.returncode != 0:
        return False, (result.stderr or "osascript failed").strip()
    return True, (result.stdout or "").strip()


# ================================
# Public API
# ================================
def list_installed_apps(limit: int = 200) -> list[str]:
    """Return the names of apps under /Applications/, /System/Applications/, ~/Applications/."""
    seen = set()
    out = []
    for d in APPLICATIONS_DIRS:
        if not os.path.isdir(d):
            continue
        try:
            for entry in sorted(os.listdir(d)):
                if not entry.endswith(".app"):
                    continue
                name = entry[:-4]
                if name in seen:
                    continue
                seen.add(name)
                out.append(name)
                if len(out) >= limit:
                    return out
        except OSError:
            continue
    return out


def list_running_apps() -> list[str]:
    """Return the names of currently visible (foreground) apps."""
    script = (
        'tell application "System Events" to '
        'get name of every application process whose background only is false'
    )
    ok, output = _run_osascript(script)
    if not ok:
        return []
    # AppleScript returns "Arc, Safari, Mail" or similar
    return [s.strip() for s in output.split(",") if s.strip()]


def get_active_app() -> Optional[str]:
    """Return the name of the frontmost app, or None on failure."""
    script = (
        'tell application "System Events" to '
        'get name of first application process whose frontmost is true'
    )
    ok, output = _run_osascript(script)
    if not ok or not output:
        return None
    return output


def get_active_window_title() -> Optional[str]:
    """Return the title of the active window of the frontmost app."""
    script = (
        'tell application "System Events" to '
        'tell (first application process whose frontmost is true) to '
        'try\n'
        '    return name of front window\n'
        'on error\n'
        '    return ""\n'
        'end try'
    )
    ok, output = _run_osascript(script)
    if not ok:
        return None
    return output or None


def launch_app(name: str) -> dict:
    """
    Launch an app by name. Returns a structured result.
    Capability check should already have happened in oz_runtime.
    """
    safe = _safe_app_name(name)
    if safe is None:
        return {"ok": False, "error": "invalid app name"}
    if not _app_exists(safe):
        return {"ok": False, "error": f"app not installed: {safe}"}

    quoted = _quote_applescript_string(safe)
    script = f'tell application "{quoted}" to activate'
    ok, output = _run_osascript(script, timeout=15.0)
    if not ok:
        return {"ok": False, "error": output or "launch failed"}
    return {"ok": True, "app": safe, "action": "launched"}


def focus_app(name: str) -> dict:
    """Bring an already-running app to the front."""
    safe = _safe_app_name(name)
    if safe is None:
        return {"ok": False, "error": "invalid app name"}

    quoted = _quote_applescript_string(safe)
    script = f'tell application "{quoted}" to activate'
    ok, output = _run_osascript(script)
    if not ok:
        return {"ok": False, "error": output or "focus failed"}
    return {"ok": True, "app": safe, "action": "focused"}


def quit_app(name: str) -> dict:
    """Quit an app gracefully (not force-kill)."""
    safe = _safe_app_name(name)
    if safe is None:
        return {"ok": False, "error": "invalid app name"}

    quoted = _quote_applescript_string(safe)
    script = f'tell application "{quoted}" to quit'
    ok, output = _run_osascript(script)
    if not ok:
        return {"ok": False, "error": output or "quit failed"}
    return {"ok": True, "app": safe, "action": "quit"}


# ================================
# CLI for testing
# ================================
def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(description="OZ macOS bridge CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("installed", help="List installed apps")
    sub.add_parser("running", help="List running (visible) apps")
    sub.add_parser("active", help="Get active app + window")

    launch_p = sub.add_parser("launch")
    launch_p.add_argument("app")

    focus_p = sub.add_parser("focus")
    focus_p.add_argument("app")

    quit_p = sub.add_parser("quit")
    quit_p.add_argument("app")

    args = parser.parse_args()

    if args.cmd == "installed":
        for a in list_installed_apps():
            print(a)
    elif args.cmd == "running":
        for a in list_running_apps():
            print(a)
    elif args.cmd == "active":
        print(json.dumps({
            "app": get_active_app(),
            "window": get_active_window_title(),
        }, indent=2, ensure_ascii=False))
    elif args.cmd == "launch":
        print(json.dumps(launch_app(args.app), indent=2, ensure_ascii=False))
    elif args.cmd == "focus":
        print(json.dumps(focus_app(args.app), indent=2, ensure_ascii=False))
    elif args.cmd == "quit":
        print(json.dumps(quit_app(args.app), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

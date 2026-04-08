"""
oz_iphone.py — iPhone (macOS Continuity経由) との橋渡し

iPhone は macOS Continuity 機能経由で繋がっている前提:
- iCloud sync (Messages, Reminders, Calendar, Contacts, Photos)
- Handoff
- AirDrop

このモジュールは AppleScript / macOS のローカル DB / Photos library を
読み書きする薄いラッパーを提供する。すべての送信系操作はユーザー承認が必要。

セキュリティ:
- 読み取り (recent_messages, recent_photos, calendar_today) → ALWAYS
- 作成 (add_reminder) → USER_APPROVE
- 送信 (send_imessage, place_call) → USER_APPROVE
- 削除 → DENY (絶対に実行しない)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ================================
# osascript helpers
# ================================
def _run_osascript(script: str, timeout: float = 10.0) -> tuple[bool, str]:
    """Run a single AppleScript via osascript."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "osascript timed out"
    except FileNotFoundError:
        return False, "osascript not found"

    if result.returncode != 0:
        return False, (result.stderr or "osascript failed").strip()
    return True, (result.stdout or "").strip()


def _quote_as(s: str) -> str:
    """Escape a string for AppleScript double-quoted literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


_PHONE_RE = re.compile(r"^[+0-9\-\s\(\)]+$")


def _safe_phone(num: str) -> Optional[str]:
    """Validate a phone number — only digits, +, -, spaces, parens."""
    if not num:
        return None
    num = num.strip()
    if len(num) > 32 or not _PHONE_RE.match(num):
        return None
    return num


# ================================
# Messages (iMessage / SMS via Messages.app)
# ================================
def recent_messages(limit: int = 10) -> dict:
    """
    Read the most recent N messages from chat.db.
    Read-only — does not send anything.

    Returns {"ok": bool, "messages": [{"text", "from", "ts", "is_from_me"}]}
    """
    db_path = os.path.expanduser("~/Library/Messages/chat.db")
    if not os.path.exists(db_path):
        return {"ok": False, "error": "Messages chat.db not found"}

    # Read-only sqlite open. The DB is locked while Messages.app is running,
    # but `sqlite3` with mode=ro works.
    import sqlite3
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        cur = conn.cursor()
        cur.execute("""
            SELECT
                m.text,
                handle.id,
                m.is_from_me,
                m.date / 1000000000 + 978307200 AS unix_ts
            FROM message AS m
            LEFT JOIN handle ON handle.ROWID = m.handle_id
            WHERE m.text IS NOT NULL AND m.text != ''
            ORDER BY m.date DESC
            LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        return {"ok": False, "error": str(e)}

    msgs = [
        {
            "text": r[0][:200],
            "from": r[1] or "(unknown)",
            "is_from_me": bool(r[2]),
            "ts": datetime.fromtimestamp(r[3]).isoformat(timespec="seconds"),
        }
        for r in rows
    ]
    return {"ok": True, "messages": msgs}


def send_imessage(recipient: str, body: str) -> dict:
    """
    Send an iMessage. Recipient can be a phone number or email (handled by
    Messages.app's normal routing). Body is escaped for AppleScript.
    """
    if not recipient or not body:
        return {"ok": False, "error": "recipient and body required"}
    if len(body) > 1000:
        return {"ok": False, "error": "body too long (>1000 chars)"}

    # Validate recipient — must look like phone or email
    is_phone = _safe_phone(recipient)
    is_email = "@" in recipient and len(recipient) < 100 and "\n" not in recipient
    if not is_phone and not is_email:
        return {"ok": False, "error": "invalid recipient (not phone/email)"}

    qr = _quote_as(recipient)
    qb = _quote_as(body)
    script = (
        'tell application "Messages"\n'
        f'  set targetService to 1st service whose service type = iMessage\n'
        f'  set targetBuddy to buddy "{qr}" of targetService\n'
        f'  send "{qb}" to targetBuddy\n'
        'end tell'
    )
    ok, output = _run_osascript(script, timeout=15.0)
    if not ok:
        return {"ok": False, "error": output[:200]}
    return {"ok": True, "recipient": recipient, "sent": body}


# ================================
# Phone — FaceTime / tel:
# ================================
def place_call(phone: str) -> dict:
    """Initiate a phone call via FaceTime/Phone. Always asks user via macOS dialog."""
    safe = _safe_phone(phone)
    if safe is None:
        return {"ok": False, "error": "invalid phone number"}
    # `open tel:` triggers macOS to use FaceTime/Phone (with system confirmation)
    try:
        subprocess.run(["open", f"tel://{safe}"], check=False, timeout=5)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "phone": safe, "note": "system call dialog opened"}


def recent_calls(limit: int = 10) -> dict:
    """
    Read recent call history from CallHistory.db (macOS Continuity stores
    iPhone calls here).
    """
    db_path = os.path.expanduser(
        "~/Library/Application Support/CallHistoryDB/CallHistory.storedata"
    )
    if not os.path.exists(db_path):
        return {"ok": False, "error": "CallHistory.storedata not found"}

    import sqlite3
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        cur = conn.cursor()
        # Schema varies by macOS version. Try the common one.
        cur.execute("""
            SELECT ZADDRESS, ZDATE, ZDURATION, ZORIGINATED
            FROM ZCALLRECORD
            ORDER BY ZDATE DESC
            LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        return {"ok": False, "error": str(e)}

    # Cocoa epoch (2001-01-01) → unix
    cocoa_epoch = datetime(2001, 1, 1).timestamp()
    calls = []
    for r in rows:
        addr, ts_cocoa, duration, originated = r
        try:
            unix_ts = (ts_cocoa or 0) + cocoa_epoch
            iso = datetime.fromtimestamp(unix_ts).isoformat(timespec="seconds")
        except Exception:
            iso = ""
        calls.append({
            "address": addr or "(unknown)",
            "ts": iso,
            "duration_s": int(duration or 0),
            "originated": bool(originated),
        })
    return {"ok": True, "calls": calls}


# ================================
# Photos — read recent without disturbing
# ================================
def recent_photos(limit: int = 10) -> dict:
    """
    Get the most recent screenshots from ~/Desktop and ~/Pictures/Screenshots.
    Photos.app library is too heavy/structured to query without API.
    """
    candidates = [
        Path.home() / "Desktop",
        Path.home() / "Pictures" / "Screenshots",
        Path.home() / "Pictures",
    ]
    photos = []
    seen = set()
    for d in candidates:
        if not d.is_dir():
            continue
        try:
            for f in d.iterdir():
                if f.suffix.lower() not in (".png", ".jpg", ".jpeg", ".heic"):
                    continue
                if f.name.startswith("."):
                    continue
                if f.name in seen:
                    continue
                seen.add(f.name)
                try:
                    stat = f.stat()
                except OSError:
                    continue
                photos.append({
                    "name": f.name,
                    "path": str(f),
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                })
        except OSError:
            continue
    # Sort by modified desc
    photos.sort(key=lambda p: p["modified"], reverse=True)
    return {"ok": True, "photos": photos[:limit]}


# ================================
# Reminders — Reminders.app via AppleScript
# ================================
def add_reminder(title: str, due: Optional[str] = None, list_name: str = "Reminders") -> dict:
    """
    Add a reminder. `due` can be a string Reminders.app understands (e.g.
    "tomorrow at 7am") — we don't parse it ourselves.
    """
    if not title or len(title) > 200:
        return {"ok": False, "error": "title required (max 200)"}
    qt = _quote_as(title)
    ql = _quote_as(list_name)

    if due:
        qd = _quote_as(due)
        # Use a simple add — date parsing left to user (Reminders auto-parses)
        script = (
            'tell application "Reminders"\n'
            f'  tell list "{ql}"\n'
            f'    make new reminder with properties {{name:"{qt}", body:"{qd}"}}\n'
            '  end tell\n'
            'end tell'
        )
    else:
        script = (
            'tell application "Reminders"\n'
            f'  tell list "{ql}"\n'
            f'    make new reminder with properties {{name:"{qt}"}}\n'
            '  end tell\n'
            'end tell'
        )
    ok, output = _run_osascript(script, timeout=10.0)
    if not ok:
        return {"ok": False, "error": output[:200]}
    return {"ok": True, "title": title, "due_hint": due}


def list_reminders(list_name: str = "Reminders", include_completed: bool = False) -> dict:
    """List incomplete reminders in a given list."""
    ql = _quote_as(list_name)
    if include_completed:
        script = (
            'tell application "Reminders"\n'
            f'  tell list "{ql}"\n'
            '    set out to ""\n'
            '    repeat with r in reminders\n'
            '      set out to out & (name of r) & "|" & (completed of r as text) & "\\n"\n'
            '    end repeat\n'
            '    return out\n'
            '  end tell\n'
            'end tell'
        )
    else:
        script = (
            'tell application "Reminders"\n'
            f'  tell list "{ql}"\n'
            '    set out to ""\n'
            '    repeat with r in (reminders whose completed is false)\n'
            '      set out to out & (name of r) & "\\n"\n'
            '    end repeat\n'
            '    return out\n'
            '  end tell\n'
            'end tell'
        )
    ok, output = _run_osascript(script, timeout=10.0)
    if not ok:
        return {"ok": False, "error": output[:200]}
    items = [line for line in output.split("\n") if line]
    return {"ok": True, "list": list_name, "items": items}


# ================================
# Calendar — Calendar.app via AppleScript
# ================================
def calendar_today() -> dict:
    """
    Return today's events from Calendar.app.
    Read-only.
    """
    script = (
        'set out to ""\n'
        'set theStart to (current date) - (time of (current date))\n'
        'set theEnd to theStart + 1 * days\n'
        'tell application "Calendar"\n'
        '  repeat with cal in calendars\n'
        '    repeat with ev in (events of cal whose start date >= theStart and start date < theEnd)\n'
        '      set out to out & (summary of ev) & "|" & (start date of ev as text) & "\\n"\n'
        '    end repeat\n'
        '  end repeat\n'
        'end tell\n'
        'return out'
    )
    ok, output = _run_osascript(script, timeout=15.0)
    if not ok:
        return {"ok": False, "error": output[:200]}
    events = []
    for line in output.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|", 1)
        events.append({
            "title": parts[0],
            "start": parts[1] if len(parts) > 1 else "",
        })
    return {"ok": True, "events": events}


# ================================
# Contacts — read-only search
# ================================
def search_contacts(query: str, limit: int = 5) -> dict:
    """Search contacts by name."""
    if not query or len(query) > 64:
        return {"ok": False, "error": "query required (max 64)"}
    qq = _quote_as(query)
    script = (
        'set out to ""\n'
        'tell application "Contacts"\n'
        f'  set matched to (people whose name contains "{qq}")\n'
        f'  repeat with i from 1 to (count of matched)\n'
        f'    if i > {limit} then exit repeat\n'
        '    set p to item i of matched\n'
        '    set out to out & (name of p) & "\\n"\n'
        '  end repeat\n'
        'end tell\n'
        'return out'
    )
    ok, output = _run_osascript(script, timeout=10.0)
    if not ok:
        return {"ok": False, "error": output[:200]}
    names = [line for line in output.split("\n") if line]
    return {"ok": True, "contacts": names}


# ================================
# AirPods / audio device detection
# ================================
def current_audio_output() -> dict:
    """Return the name of the currently active audio output device."""
    try:
        result = subprocess.run(
            ["system_profiler", "SPAudioDataType"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    text = result.stdout
    # Look for "Default Output Device: Yes" and the surrounding device name
    lines = text.split("\n")
    current = None
    for i, line in enumerate(lines):
        if "Default Output Device: Yes" in line:
            # Walk backward to find the device name (usually the line above some indented block)
            for j in range(i, max(0, i - 15), -1):
                stripped = lines[j].strip()
                if stripped.endswith(":") and not stripped.startswith(("Default", "Manufacturer", "Output", "Transport")):
                    current = stripped.rstrip(":")
                    break
            break
    return {"ok": True, "device": current or "(unknown)"}


def airpods_connected() -> dict:
    """Check if AirPods are currently connected (Bluetooth)."""
    try:
        result = subprocess.run(
            ["system_profiler", "SPBluetoothDataType"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    text = result.stdout.lower()
    has_airpods = ("airpods" in text)
    # Detect if currently connected
    connected = False
    if has_airpods:
        # Look for "Connected: Yes" near "AirPods"
        idx = text.find("airpods")
        snippet = text[idx:idx + 800]
        if "connected: yes" in snippet:
            connected = True
    return {"ok": True, "airpods_present": has_airpods, "connected": connected}


# ================================
# CLI for inspection
# ================================
def main():
    import argparse
    parser = argparse.ArgumentParser(description="OZ iPhone bridge CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("messages")
    sub.add_parser("calls")
    sub.add_parser("photos")
    sub.add_parser("today")
    sub.add_parser("audio")
    sub.add_parser("airpods")

    rm = sub.add_parser("remind")
    rm.add_argument("title")
    rm.add_argument("--due", default=None)

    list_rm = sub.add_parser("list-reminders")
    list_rm.add_argument("--list", default="Reminders")

    contacts = sub.add_parser("contacts")
    contacts.add_argument("query")

    args = parser.parse_args()

    if args.cmd == "messages":
        print(json.dumps(recent_messages(), indent=2, ensure_ascii=False))
    elif args.cmd == "calls":
        print(json.dumps(recent_calls(), indent=2, ensure_ascii=False))
    elif args.cmd == "photos":
        print(json.dumps(recent_photos(), indent=2, ensure_ascii=False))
    elif args.cmd == "today":
        print(json.dumps(calendar_today(), indent=2, ensure_ascii=False))
    elif args.cmd == "audio":
        print(json.dumps(current_audio_output(), indent=2, ensure_ascii=False))
    elif args.cmd == "airpods":
        print(json.dumps(airpods_connected(), indent=2, ensure_ascii=False))
    elif args.cmd == "remind":
        print(json.dumps(add_reminder(args.title, due=args.due), indent=2, ensure_ascii=False))
    elif args.cmd == "list-reminders":
        print(json.dumps(list_reminders(args.list), indent=2, ensure_ascii=False))
    elif args.cmd == "contacts":
        print(json.dumps(search_contacts(args.query), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

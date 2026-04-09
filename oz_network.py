"""
oz_network.py — Joe専用のパーソナル人間関係グラフ

iMessage / Mail / Contacts から「人」と「親密度」を集計し、
3D空間に配置できる JSON を出力する。

データソース (read-only):
- ~/Library/Messages/chat.db          → iMessage / SMS 履歴
- ~/Library/Mail/V*/                  → Mail.app の mbox (将来)
- Contacts.app                        → 名前マッピング (AppleScript)

スコアリング:
  intimacy = recency * 0.5 + frequency * 0.3 + diversity * 0.2

すべてローカル SQLite 読み取りのみ。外部送信なし。
"""

from __future__ import annotations

import email
import email.header
import email.utils
import json
import math
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ================================
# Constants
# ================================
MESSAGES_DB = os.path.expanduser("~/Library/Messages/chat.db")
MAIL_ROOT = os.path.expanduser("~/Library/Mail")
NETWORK_CACHE = os.path.expanduser("~/.openclaw/oz_vault/network_snapshot.json")

# Joe's own email addresses — never include them as contacts
SELF_EMAILS = {
    "joemekw@gmail.com",
    "yuusukew18@gmail.com",
    "maekawasei@gmail.com",
}

# Time windows
RECENT_DAYS = 90
ACTIVE_DAYS = 7
WARM_DAYS = 30


# ================================
# iMessage data
# ================================
def _read_imessage_contacts(limit: int = 200) -> list[dict]:
    """
    Read all iMessage contacts and aggregate stats from chat.db.

    Returns one entry per handle (phone or email):
    {
      "handle": "+8190...",
      "messages_total": 234,
      "messages_recent": 18,    # past 90 days
      "last_contact_ts": 1775612000,
      "is_from_me_ratio": 0.4,
    }
    """
    if not os.path.exists(MESSAGES_DB):
        return []

    try:
        uri = f"file:{MESSAGES_DB}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        cur = conn.cursor()

        # iMessage stores dates as nanoseconds since 2001-01-01
        # Convert to unix epoch: ns / 1e9 + 978307200
        recent_threshold_unix = time.time() - (RECENT_DAYS * 86400)
        recent_threshold_apple = (recent_threshold_unix - 978307200) * 1_000_000_000

        cur.execute("""
            SELECT
                handle.id AS handle,
                COUNT(*) AS total,
                SUM(CASE WHEN m.date > ? THEN 1 ELSE 0 END) AS recent,
                MAX(m.date) AS last_date,
                AVG(m.is_from_me) AS from_me_ratio
            FROM message AS m
            LEFT JOIN handle ON handle.ROWID = m.handle_id
            WHERE handle.id IS NOT NULL AND m.text IS NOT NULL
            GROUP BY handle.id
            ORDER BY recent DESC, total DESC
            LIMIT ?
        """, (recent_threshold_apple, limit))
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        print(f"  imessage read error: {e}")
        return []

    contacts = []
    for handle, total, recent, last_date, from_me in rows:
        if last_date is None:
            continue
        last_unix = (last_date / 1_000_000_000) + 978307200
        contacts.append({
            "handle": handle,
            "channel": "imessage",
            "messages_total": total or 0,
            "messages_recent": recent or 0,
            "last_contact_ts": last_unix,
            "from_me_ratio": from_me or 0.0,
        })
    return contacts


# ================================
# Mail.app — emlx mbox parsing
# ================================
# Each email is one .emlx file: first line = size in bytes, then RFC822 message.
# We extract just From, To, Cc, Date, Subject. Body is ignored (no PII leaks).

_EMAIL_RE = re.compile(r"<([^>]+@[^>]+)>")
_RAW_EMAIL_RE = re.compile(r"([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)")


def _decode_header(raw: str) -> str:
    """Decode an MIME-encoded header to a plain string."""
    if not raw:
        return ""
    try:
        parts = email.header.decode_header(raw)
        out = []
        for chunk, charset in parts:
            if isinstance(chunk, bytes):
                try:
                    out.append(chunk.decode(charset or "utf-8", errors="replace"))
                except (LookupError, AttributeError):
                    out.append(chunk.decode("utf-8", errors="replace"))
            else:
                out.append(chunk)
        return "".join(out)
    except Exception:
        return raw


def _extract_addresses(header_value: str) -> list[tuple[str, str]]:
    """Return [(name, email), ...] from a To/From/Cc header."""
    if not header_value:
        return []
    decoded = _decode_header(header_value)
    parsed = email.utils.getaddresses([decoded])
    out = []
    for name, addr in parsed:
        if not addr or "@" not in addr:
            # Sometimes addresses come without brackets
            m = _RAW_EMAIL_RE.search(decoded)
            if m:
                addr = m.group(1)
        if addr and "@" in addr:
            out.append((name.strip(), addr.lower().strip()))
    return out


def _parse_emlx(path: Path) -> Optional[dict]:
    """
    Parse a single .emlx file. Returns {from, to, cc, date_ts, subject}
    or None on failure.
    """
    try:
        with open(path, "rb") as f:
            # First line = byte count, then the actual rfc822 content
            first_line = f.readline().strip()
            try:
                int(first_line)
            except ValueError:
                pass  # not all files have this prefix
            data = f.read()
    except OSError:
        return None

    try:
        msg = email.message_from_bytes(data)
    except Exception:
        return None

    # Date
    date_ts = 0
    raw_date = msg.get("Date", "")
    if raw_date:
        try:
            dt = email.utils.parsedate_to_datetime(raw_date)
            if dt:
                date_ts = dt.timestamp()
        except Exception:
            pass

    return {
        "from": _extract_addresses(msg.get("From", "")),
        "to": _extract_addresses(msg.get("To", "")),
        "cc": _extract_addresses(msg.get("Cc", "")),
        "date_ts": date_ts,
        "subject": _decode_header(msg.get("Subject", ""))[:100],
    }


def _read_mail_contacts(limit: int = 200, max_files: int = 5000) -> list[dict]:
    """
    Walk the Mail.app emlx files and aggregate contacts.

    For performance, we only sort + parse the most recently modified files
    (recent emails matter most for an active social graph).
    """
    if not os.path.isdir(MAIL_ROOT):
        return []

    # Find all emlx files (skip Trash, Spam, Drafts)
    candidates = []
    skip_keywords = ("trash", "spam", "junk", "drafts")
    for root, dirs, files in os.walk(MAIL_ROOT):
        if any(k in root.lower() for k in skip_keywords):
            continue
        for fname in files:
            if fname.endswith(".emlx") and not fname.endswith(".partial.emlx"):
                candidates.append(os.path.join(root, fname))

    # Sort by mtime, take the most recent N
    try:
        candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    except OSError:
        pass
    candidates = candidates[:max_files]

    now_ts = time.time()
    recent_threshold = now_ts - (RECENT_DAYS * 86400)

    # contacts: {email: {name, total, recent, last_ts, sent_count, received_count}}
    contacts: dict[str, dict] = {}

    for path in candidates:
        meta = _parse_emlx(Path(path))
        if meta is None or meta["date_ts"] == 0:
            continue

        date_ts = meta["date_ts"]
        is_recent = date_ts >= recent_threshold

        # Determine if this email was sent BY Joe or TO Joe
        from_addrs = meta["from"]
        is_sent_by_joe = any(addr in SELF_EMAILS for _, addr in from_addrs)

        if is_sent_by_joe:
            # Joe sent it — count the recipients
            for name, addr in meta["to"] + meta["cc"]:
                if addr in SELF_EMAILS:
                    continue
                c = contacts.setdefault(addr, {
                    "handle": addr,
                    "channel": "mail",
                    "name": name,
                    "messages_total": 0,
                    "messages_recent": 0,
                    "last_contact_ts": 0,
                    "sent_count": 0,
                    "received_count": 0,
                })
                c["messages_total"] += 1
                if is_recent:
                    c["messages_recent"] += 1
                if date_ts > c["last_contact_ts"]:
                    c["last_contact_ts"] = date_ts
                    if name and not c["name"]:
                        c["name"] = name
                c["sent_count"] += 1
        else:
            # Joe received it — count the sender
            for name, addr in from_addrs:
                if addr in SELF_EMAILS:
                    continue
                c = contacts.setdefault(addr, {
                    "handle": addr,
                    "channel": "mail",
                    "name": name,
                    "messages_total": 0,
                    "messages_recent": 0,
                    "last_contact_ts": 0,
                    "sent_count": 0,
                    "received_count": 0,
                })
                c["messages_total"] += 1
                if is_recent:
                    c["messages_recent"] += 1
                if date_ts > c["last_contact_ts"]:
                    c["last_contact_ts"] = date_ts
                    if name and not c["name"]:
                        c["name"] = name
                c["received_count"] += 1

    # Compute from_me_ratio and filter to top
    out = []
    for c in contacts.values():
        total = c["sent_count"] + c["received_count"]
        c["from_me_ratio"] = c["sent_count"] / total if total > 0 else 0.0
        out.append(c)

    # Sort by recent activity
    out.sort(key=lambda c: (c["messages_recent"], c["messages_total"]), reverse=True)
    return out[:limit]


# ================================
# Contacts.app — name lookup
# ================================
def _contact_names_for_handles(handles: list[str]) -> dict[str, str]:
    """
    Resolve handles (phone numbers, emails) to display names via Contacts.app.

    AppleScript is slow per-call so we batch in one script.
    """
    if not handles:
        return {}

    # Build an AppleScript that searches each handle and prints "handle|name"
    script_parts = ['set out to ""', 'tell application "Contacts"']
    for h in handles[:50]:  # cap to avoid huge scripts
        # Escape the handle for AS
        h_safe = h.replace("\\", "\\\\").replace('"', '\\"')
        script_parts.append(f'  try')
        script_parts.append(
            f'    set p to (first person whose '
            f'value of phones contains "{h_safe}" or '
            f'value of emails contains "{h_safe}")'
        )
        script_parts.append(f'    set out to out & "{h_safe}" & "|" & (name of p) & "\\n"')
        script_parts.append(f'  end try')
    script_parts.append("end tell")
    script_parts.append("return out")

    script = "\n".join(script_parts)
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return {}

    if result.returncode != 0:
        return {}

    mapping = {}
    for line in result.stdout.split("\n"):
        if not line or "|" not in line:
            continue
        h, name = line.split("|", 1)
        if h and name:
            mapping[h] = name
    return mapping


# ================================
# Intimacy scoring
# ================================
def _intimacy_score(contact: dict, now_ts: float) -> float:
    """
    Calculate an intimacy score in [0, 1].

    Combines:
    - recency: how recently we last spoke (decays exponentially)
    - frequency: messages in the past 90 days (log scale)
    - direct_ratio: balanced exchange = higher
    """
    last_ts = contact.get("last_contact_ts", 0)
    if last_ts <= 0:
        return 0.0

    days_ago = (now_ts - last_ts) / 86400
    recency = math.exp(-days_ago / 30)  # half-life ~21 days

    recent = contact.get("messages_recent", 0)
    frequency = min(1.0, math.log1p(recent) / math.log1p(50))

    from_me = contact.get("from_me_ratio", 0.5)
    # 0.5 = fully balanced, 0 or 1 = one-way
    balance = 1.0 - 2 * abs(from_me - 0.5)

    score = recency * 0.5 + frequency * 0.35 + balance * 0.15
    return max(0.0, min(1.0, score))


# ================================
# 3D placement
# ================================
def _spherical_position(contact: dict, intimacy: float, idx: int, total: int) -> dict:
    """
    Map a contact to a 3D position around Joe (origin).

    Distance: inverse of intimacy (close people = small radius)
    Angle: based on channel (different channels = different quadrants)
    Height: based on activity recency
    """
    # Radius: closer = smaller
    radius = 8 + (1.0 - intimacy) * 35

    # Theta: distribute evenly around the circle, then offset by channel
    base_theta = (idx / max(1, total)) * 2 * math.pi
    channel_offsets = {
        "imessage": 0,
        "mail": math.pi / 2,
        "slack": math.pi,
        "line": 3 * math.pi / 2,
    }
    theta = base_theta + channel_offsets.get(contact.get("channel", "imessage"), 0) * 0.1

    x = math.cos(theta) * radius
    z = math.sin(theta) * radius

    # Height: how active recently
    last_ts = contact.get("last_contact_ts", 0)
    days_ago = (time.time() - last_ts) / 86400 if last_ts > 0 else 999
    if days_ago < ACTIVE_DAYS:
        y = 6
    elif days_ago < WARM_DAYS:
        y = 2
    else:
        y = -1

    return {"x": round(x, 2), "y": round(y, 2), "z": round(z, 2)}


# ================================
# Public API
# ================================
def build_network(limit: int = 80, with_names: bool = False, max_mail_files: int = 5000) -> dict:
    """
    Build the personal network snapshot. Read-only.

    Combines iMessage + Mail.app emlx files into a unified contact graph.

    Returns:
    {
      "generated_at": "...",
      "you": {"position": {"x":0, "y":0, "z":0}, "label": "Joe"},
      "contacts": [...],
      "stats": {...},
    }
    """
    now = time.time()

    # === Source 1: iMessage chat.db ===
    imessage_contacts = _read_imessage_contacts(limit=limit)

    # === Source 2: Mail.app emlx files ===
    mail_contacts = _read_mail_contacts(limit=limit, max_files=max_mail_files)

    # === Merge ===
    # Use handle (phone or email) as key. Mail and iMessage rarely overlap
    # in the handle, so they merge cleanly.
    all_contacts: dict[str, dict] = {}
    for c in imessage_contacts + mail_contacts:
        existing = all_contacts.get(c["handle"])
        if existing is None:
            all_contacts[c["handle"]] = c
        else:
            # Merge — sum counts, take max date, prefer the channel with more activity
            existing["messages_total"] += c.get("messages_total", 0)
            existing["messages_recent"] += c.get("messages_recent", 0)
            existing["last_contact_ts"] = max(
                existing.get("last_contact_ts", 0),
                c.get("last_contact_ts", 0),
            )
            if not existing.get("name") and c.get("name"):
                existing["name"] = c["name"]

    contacts_list = list(all_contacts.values())

    if not contacts_list:
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "you": {"position": {"x": 0, "y": 0, "z": 0}, "label": "Joe"},
            "contacts": [],
            "stats": {
                "total_handles": 0,
                "imessage_count": len(imessage_contacts),
                "mail_count": len(mail_contacts),
                "high_intimacy_count": 0,
            },
        }

    # Score everyone
    for c in contacts_list:
        c["intimacy"] = _intimacy_score(c, now)
    contacts_list.sort(key=lambda c: -c["intimacy"])

    # Optionally enrich with Contacts.app names (slow, often times out)
    if with_names:
        handles = [c["handle"] for c in contacts_list[:30]]
        name_map = _contact_names_for_handles(handles)
        for c in contacts_list:
            if c["handle"] in name_map:
                c["name"] = name_map[c["handle"]]

    # Trim to top N for display
    contacts_list = contacts_list[:limit]

    # Place in 3D
    placed = []
    for idx, c in enumerate(contacts_list):
        # Fallback name = local part of email or first word of handle
        if not c.get("name"):
            handle = c.get("handle", "?")
            c["name"] = handle.split("@")[0] if "@" in handle else handle

        c["position"] = _spherical_position(c, c["intimacy"], idx, len(contacts_list))
        last_ts = c.get("last_contact_ts", 0)
        c["last_contact_iso"] = (
            datetime.fromtimestamp(last_ts).isoformat(timespec="seconds")
            if last_ts > 0 else ""
        )
        placed.append(c)

    snapshot = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "you": {"position": {"x": 0, "y": 0, "z": 0}, "label": "Joe"},
        "contacts": placed,
        "stats": {
            "total_handles": len(placed),
            "imessage_count": len(imessage_contacts),
            "mail_count": len(mail_contacts),
            "high_intimacy_count": sum(1 for c in placed if c["intimacy"] > 0.5),
        },
    }
    return snapshot


def save_snapshot(snapshot: dict) -> str:
    """Cache the snapshot to vault for the frontend to read."""
    Path(NETWORK_CACHE).parent.mkdir(parents=True, exist_ok=True)
    Path(NETWORK_CACHE).write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        os.chmod(NETWORK_CACHE, 0o600)
    except OSError:
        pass
    return NETWORK_CACHE


def load_snapshot() -> Optional[dict]:
    """Load the most recent cached snapshot."""
    if not os.path.exists(NETWORK_CACHE):
        return None
    try:
        return json.loads(Path(NETWORK_CACHE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ================================
# CLI
# ================================
def main():
    import argparse
    parser = argparse.ArgumentParser(description="OZ personal network builder")
    sub = parser.add_subparsers(dest="cmd", required=True)

    build_p = sub.add_parser("build")
    build_p.add_argument("--limit", type=int, default=80)
    build_p.add_argument("--no-names", action="store_true")
    build_p.add_argument("--save", action="store_true")

    sub.add_parser("show")

    args = parser.parse_args()

    if args.cmd == "build":
        snap = build_network(limit=args.limit, with_names=not args.no_names)
        if args.save:
            path = save_snapshot(snap)
            print(f"saved to {path}")
        # Print summary
        print(f"Generated: {snap['generated_at']}")
        print(f"Contacts: {len(snap['contacts'])}")
        print(f"High intimacy: {snap['stats']['high_intimacy_count']}")
        print()
        print("Top 10:")
        for c in snap["contacts"][:10]:
            name = c.get("name", c["handle"])[:30]
            print(f"  {name:32} intimacy={c['intimacy']:.2f}  recent={c['messages_recent']}  total={c['messages_total']}")
    elif args.cmd == "show":
        snap = load_snapshot()
        if snap is None:
            print("no snapshot — run `build --save` first")
        else:
            print(json.dumps(snap, indent=2, ensure_ascii=False)[:2000])


if __name__ == "__main__":
    main()

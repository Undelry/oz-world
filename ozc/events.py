"""
oz_events.py — Signed event log (Phase 3)

The unified record of everything that happens in OZ.

Every significant action — place publish, skill publish, skill rating,
place install, agent ask, network-wide announcements — is recorded as a
**signed event**. Events are the primary data model for OZ's federation
layer: they carry their own cryptographic provenance, so they can be
freely passed between OZ instances without trusting the transport.

Think of this as OZ's version of Nostr events, or Git commits, or
append-only logs. The ledger (oz_economy) is a specialized subset that
tracks OZC balances; events are the general mechanism.

Design principles:
- **Append-only**: events are never modified or deleted. A correction is
  a new event that supersedes the old one.
- **Self-contained**: every event includes its signer's pubkey + signature,
  so a verifier needs only this one row to trust it.
- **Typed payload**: the `type` field (e.g. "place.publish", "skill.rate")
  identifies how to interpret `payload`.
- **Hash chained** (optional): like the ledger, events can be linked into
  a tamper-evident chain per signer.
- **Relay-friendly**: the wire format is exactly the row format, so syncing
  between OZ instances is trivial.

Schema:
  events
    id              INTEGER PRIMARY KEY
    event_uid       TEXT UNIQUE  -- stable id: sha256(signer || ts || type || payload)
    type            TEXT         -- e.g. "place.publish", "skill.rate"
    payload_json    TEXT         -- canonical JSON of the payload
    signer_pubkey   TEXT         -- 64-char hex Ed25519 pubkey
    sig             TEXT         -- 128-char hex signature
    ts              REAL         -- timestamp (from signer's clock)
    received_at     REAL         -- when THIS relay received it (local clock)
    source_relay    TEXT         -- null = local origin; else URL of upstream
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from ozc import identity as oz_identity

ROOT = Path(os.path.expanduser("~/.openclaw"))
DB_PATH = ROOT / "oz_events.db"

_lock = threading.Lock()


# ================================
# DB setup
# ================================
def _conn():
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    ROOT.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(ROOT, 0o700)
    except OSError:
        pass
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uid TEXT UNIQUE NOT NULL,
            type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            signer_pubkey TEXT NOT NULL,
            sig TEXT NOT NULL,
            ts REAL NOT NULL,
            received_at REAL NOT NULL,
            source_relay TEXT
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_type ON events(type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_signer ON events(signer_pubkey)")
    conn.close()
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass


# ================================
# Canonical serialization
# ================================
def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _compute_event_uid(signer_pubkey: str, ts: float, event_type: str, payload_json: str) -> str:
    """Deterministic id: sha256 of signer + ts + type + payload."""
    raw = f"{signer_pubkey}|{ts:.6f}|{event_type}|{payload_json}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _build_signing_bytes(ts: float, event_type: str, payload_json: str, signer_pubkey: str) -> bytes:
    """Build the exact bytes that get signed. Deterministic."""
    return f"{ts:.6f}|{event_type}|{signer_pubkey}|{payload_json}".encode("utf-8")


# ================================
# Publishing
# ================================
def publish_event(event_type: str, payload: dict) -> dict:
    """
    Sign and store a new event with the local identity.

    Returns the full event row. Fails if no identity is present.
    """
    init_db()
    if not oz_identity.has_identity():
        raise RuntimeError("no identity — run 'python3 -m ozc identity init' first")

    signer_pubkey = oz_identity.public_key_hex()
    ts = time.time()
    payload_json = _canonical(payload)
    signing_bytes = _build_signing_bytes(ts, event_type, payload_json, signer_pubkey)
    sig = oz_identity.sign_hex(signing_bytes)
    event_uid = _compute_event_uid(signer_pubkey, ts, event_type, payload_json)

    with _lock:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO events (event_uid, type, payload_json, signer_pubkey, sig, ts, received_at, source_relay)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
        """, (event_uid, event_type, payload_json, signer_pubkey, sig, ts, time.time()))
        row_id = cur.lastrowid
        conn.close()

    return {
        "id": row_id,
        "event_uid": event_uid,
        "type": event_type,
        "payload": payload,
        "signer_pubkey": signer_pubkey,
        "sig": sig,
        "ts": ts,
        "source_relay": None,
    }


# ================================
# Verification
# ================================
def verify_event_row(row: dict) -> bool:
    """
    Verify that a row's signature matches its content.

    Expects a row with: type, payload_json (string), signer_pubkey, sig, ts
    """
    signing_bytes = _build_signing_bytes(
        float(row["ts"]),
        row["type"],
        row["payload_json"],
        row["signer_pubkey"],
    )
    return oz_identity.verify_hex(signing_bytes, row["sig"], row["signer_pubkey"])


def verify_all_events() -> dict:
    """Walk the whole events table and verify every row's signature."""
    init_db()
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT id, type, payload_json, signer_pubkey, sig, ts FROM events ORDER BY id ASC")
    rows = cur.fetchall()
    conn.close()

    total = 0
    valid = 0
    broken = []
    for row in rows:
        total += 1
        r = {
            "id": row[0],
            "type": row[1],
            "payload_json": row[2],
            "signer_pubkey": row[3],
            "sig": row[4],
            "ts": row[5],
        }
        if verify_event_row(r):
            valid += 1
        else:
            broken.append(row[0])

    return {
        "ok": valid == total,
        "total": total,
        "valid": valid,
        "broken_ids": broken,
    }


# ================================
# Receiving from another relay
# ================================
def accept_remote_event(event: dict, source_relay: str) -> dict:
    """
    Called when an event arrives from another relay. We verify the signature
    against the signer's claimed pubkey, check for duplicates by event_uid,
    then store it with source_relay != NULL.
    """
    init_db()

    required = ("type", "payload", "signer_pubkey", "sig", "ts")
    for k in required:
        if k not in event:
            return {"ok": False, "error": f"missing field: {k}"}

    payload_json = _canonical(event["payload"])
    row = {
        "type": event["type"],
        "payload_json": payload_json,
        "signer_pubkey": event["signer_pubkey"],
        "sig": event["sig"],
        "ts": event["ts"],
    }
    if not verify_event_row(row):
        return {"ok": False, "error": "signature invalid"}

    event_uid = _compute_event_uid(
        event["signer_pubkey"], float(event["ts"]),
        event["type"], payload_json,
    )

    with _lock:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT id FROM events WHERE event_uid = ?", (event_uid,))
        if cur.fetchone():
            conn.close()
            return {"ok": True, "duplicate": True, "event_uid": event_uid}
        cur.execute("""
            INSERT INTO events (event_uid, type, payload_json, signer_pubkey, sig, ts, received_at, source_relay)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (event_uid, event["type"], payload_json,
              event["signer_pubkey"], event["sig"], event["ts"],
              time.time(), source_relay))
        conn.close()

    return {"ok": True, "event_uid": event_uid, "stored": True}


# ================================
# Query
# ================================
def list_events(
    since_ts: Optional[float] = None,
    event_type: Optional[str] = None,
    signer: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    init_db()
    conn = _conn()
    cur = conn.cursor()
    clauses = []
    params = []
    if since_ts is not None:
        clauses.append("ts > ?")
        params.append(since_ts)
    if event_type is not None:
        clauses.append("type = ?")
        params.append(event_type)
    if signer is not None:
        clauses.append("signer_pubkey = ?")
        params.append(signer)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    cur.execute(f"""
        SELECT id, event_uid, type, payload_json, signer_pubkey, sig, ts, received_at, source_relay
        FROM events {where} ORDER BY ts DESC LIMIT ?
    """, params)
    rows = cur.fetchall()
    conn.close()

    out = []
    for r in rows:
        try:
            payload = json.loads(r[3])
        except json.JSONDecodeError:
            payload = {}
        out.append({
            "id": r[0],
            "event_uid": r[1],
            "type": r[2],
            "payload": payload,
            "signer_pubkey": r[4],
            "sig": r[5],
            "ts": r[6],
            "received_at": r[7],
            "source_relay": r[8],
        })
    return out


def stats() -> dict:
    init_db()
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM events")
    total = cur.fetchone()[0]
    cur.execute("SELECT type, COUNT(*) FROM events GROUP BY type ORDER BY 2 DESC")
    by_type = dict(cur.fetchall())
    cur.execute("SELECT COUNT(DISTINCT signer_pubkey) FROM events")
    unique_signers = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM events WHERE source_relay IS NOT NULL")
    remote_count = cur.fetchone()[0]
    conn.close()
    return {
        "total": total,
        "by_type": by_type,
        "unique_signers": unique_signers,
        "remote_events": remote_count,
    }


# ================================
# CLI
# ================================
def main():
    import argparse
    parser = argparse.ArgumentParser(description="OZ Events (signed event log)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    sub.add_parser("stats")
    sub.add_parser("verify")

    pub = sub.add_parser("publish")
    pub.add_argument("type")
    pub.add_argument("payload_json")

    lst = sub.add_parser("list")
    lst.add_argument("--since", type=float, default=None)
    lst.add_argument("--type", default=None)
    lst.add_argument("--signer", default=None)
    lst.add_argument("--limit", type=int, default=20)

    args = parser.parse_args()

    if args.cmd == "init":
        init_db()
        print(f"initialized at {DB_PATH}")
    elif args.cmd == "stats":
        print(json.dumps(stats(), indent=2, ensure_ascii=False))
    elif args.cmd == "verify":
        print(json.dumps(verify_all_events(), indent=2, ensure_ascii=False))
    elif args.cmd == "publish":
        payload = json.loads(args.payload_json)
        result = publish_event(args.type, payload)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.cmd == "list":
        events = list_events(
            since_ts=args.since,
            event_type=args.type,
            signer=args.signer,
            limit=args.limit,
        )
        for e in events:
            short = json.dumps(e["payload"], ensure_ascii=False)[:60]
            print(f"  {e['id']:5}  {e['type']:20}  {e['signer_pubkey'][:16]}  {short}")


if __name__ == "__main__":
    main()

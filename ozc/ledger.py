"""
oz_economy.py — OZ内部経済システム (Phase 1)

OZの全エージェントとリソース消費を「OZコイン (OZC)」で管理する。
全てのアクション（LLM呼び出し、TTS、音声認識、ファイル操作等）に
コストを設定し、ワーカー間の取引を SQLite ベースの台帳に記録する。

設計:
- ワーカーごとの口座 (balances)
- 全トランザクション履歴 (ledger)
- アクションごとの単価 (PRICE_TABLE)
- 1日の総消費上限 (daily cap)

API は oz_webserver.py 経由で公開する想定。
このモジュール単体でも CLI から動作確認可能。
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime
from typing import Optional

DB_PATH = os.path.expanduser("~/.openclaw/workspace/oz_economy.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)


def _secure_db_perms():
    """Ensure the ledger DB is readable/writable only by the owner."""
    for path in (DB_PATH, DB_PATH + "-wal", DB_PATH + "-shm"):
        if os.path.exists(path):
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

# 1 OZC ≈ ¥1 (the exchange rate to real currency)
OZC_TO_JPY = 1.0


def ozc_to_jpy(amount: float) -> float:
    return amount * OZC_TO_JPY

# ================================
# Pricing — actions and their cost
# ================================
PRICE_TABLE = {
    # LLM
    "llm.claude.call": 5,           # 1 Claude API call
    "llm.gpt.call": 4,              # 1 GPT API call
    "llm.local.call": 1,            # local model call

    # Speech
    "tts.speak": 1,                 # macOS say
    "stt.transcribe": 2,            # Whisper transcribe

    # IO
    "file.read": 0.1,
    "file.write": 0.2,
    "http.fetch": 0.5,

    # External actions
    "email.send": 10,
    "message.send": 5,
    "app.launch": 2,
    "screen.capture": 1,

    # Reporting
    "report.human": 0.5,            # ワーカーが人間に何か伝える
    "task.complete": 0,             # 完了通知 (free)
}

# ================================
# Initial agent balances
# ================================
INITIAL_BALANCES = {
    "hitomi": 10000,        # オーケストレーター。予算配分役
    "coder": 500,
    "researcher": 300,
    "reviewer": 200,
    "debugger": 400,
    "writer": 250,
    "scheduler": 150,
    "macos-bridge": 200,    # macOS操作担当 (CLI 経由で osascript)
    "iphone-bridge": 200,   # iPhone操作担当 (Continuity 経由)
    "human": 100000,        # ユーザー本人 (仮想口座)
    "treasury": 1000000,    # システム口座 (鋳造元)
}

# 1日の総消費上限 (全エージェント合算)
DAILY_BUDGET_CAP = 5000

# 1ヶ月の実通貨上限 (¥) — 暴走の最後の防波堤
MONTHLY_REAL_CAP_JPY = 3000


# ================================
# Database setup
# ================================
_lock = threading.Lock()


def _conn():
    """Get a thread-safe SQLite connection."""
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Create tables and seed initial balances if needed."""
    with _lock:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS balances (
                agent TEXT PRIMARY KEY,
                balance REAL NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                from_agent TEXT NOT NULL,
                to_agent TEXT NOT NULL,
                amount REAL NOT NULL,
                action TEXT NOT NULL,
                detail TEXT,
                from_balance_after REAL,
                to_balance_after REAL,
                prev_hash TEXT,
                hash TEXT,
                signer_pubkey TEXT,    -- Phase 2: Ed25519 pubkey of who signed this tx
                sig TEXT               -- Phase 2: hex-encoded Ed25519 signature
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_ledger_ts ON ledger(ts DESC)
        """)
        # Add columns if upgrading from older schema
        for col in ("prev_hash", "hash", "signer_pubkey", "sig"):
            try:
                cur.execute(f"ALTER TABLE ledger ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass

        # Seed initial balances if table is empty
        cur.execute("SELECT COUNT(*) FROM balances")
        if cur.fetchone()[0] == 0:
            for agent, balance in INITIAL_BALANCES.items():
                cur.execute(
                    "INSERT INTO balances (agent, balance) VALUES (?, ?)",
                    (agent, balance),
                )
        conn.close()
        _secure_db_perms()


# ================================
# Block hashing — tamper-proof ledger
# ================================
GENESIS_HASH = "0" * 64


def _compute_block_hash(tx: dict) -> str:
    """Hash a transaction record together with the previous block's hash.

    All numeric fields are normalized to float and rounded to 6 decimals so that
    int vs float and storage round-trips don't change the hash.

    Backwards compatibility (Phase 2):
    - Pre-Phase-2 rows (no signer_pubkey, no sig) use the original hash schema
      so the 1000+ existing blocks verify unchanged.
    - Phase-2+ rows with a signer add "signer" and "sig" fields to the hash
      payload, binding the signature into the chain. Stripping the sig from
      a signed row will break verification.
    """
    base = {
        "id": int(tx["id"]),
        "ts": round(float(tx["ts"]), 6),
        "from": tx["from_agent"],
        "to": tx["to_agent"],
        "amount": round(float(tx["amount"]), 6),
        "action": tx["action"],
        "detail": tx["detail"] or "",
        "prev": tx["prev_hash"],
    }
    signer = tx.get("signer_pubkey")
    sig = tx.get("sig")
    if signer:
        # Phase 2+ row: commit signer + sig to the hash
        base["signer"] = signer
        base["sig"] = sig or ""
    # Else: pre-Phase-2 row, use the original schema (no signer/sig fields)

    payload = json.dumps(base, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _get_last_hash(cur) -> str:
    cur.execute("SELECT hash FROM ledger WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    return row[0] if row and row[0] else GENESIS_HASH


def verify_chain() -> dict:
    """
    Walk the entire ledger and verify every block's hash matches its content
    and links correctly to the previous block.

    Phase 2: If a block has a signer_pubkey, the Ed25519 signature is also
    verified. Unsigned blocks (pre-Phase-2) verify by hash alone.

    Returns:
        {ok, total, valid, signed, broken_at, reason}
    """
    with _lock:
        conn = _conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, ts, from_agent, to_agent, amount, action, detail, "
            "       prev_hash, hash, signer_pubkey, sig "
            "FROM ledger ORDER BY id ASC"
        )
        rows = cur.fetchall()
        conn.close()

    try:
        from ozc import identity as oz_identity
    except ImportError:
        oz_identity = None

    expected_prev = GENESIS_HASH
    total = 0
    signed_count = 0
    for row in rows:
        total += 1
        tx = {
            "id": row[0],
            "ts": row[1],
            "from_agent": row[2],
            "to_agent": row[3],
            "amount": row[4],
            "action": row[5],
            "detail": row[6],
            "prev_hash": row[7],
            "hash": row[8],
            "signer_pubkey": row[9],
            "sig": row[10],
        }
        if tx["prev_hash"] != expected_prev:
            return {"ok": False, "total": total, "valid": total - 1, "signed": signed_count,
                    "broken_at": tx["id"], "reason": "prev_hash mismatch"}
        expected_hash = _compute_block_hash(tx)
        if tx["hash"] != expected_hash:
            return {"ok": False, "total": total, "valid": total - 1, "signed": signed_count,
                    "broken_at": tx["id"], "reason": "hash mismatch"}

        # Verify signature if present
        if tx["signer_pubkey"] and tx["sig"] and oz_identity is not None:
            # Recompute the pre-sig hash that was signed
            tx_without_sig = dict(tx)
            tx_without_sig["sig"] = None
            presig_hash = _compute_block_hash(tx_without_sig)
            if not oz_identity.verify_hex(presig_hash.encode("utf-8"),
                                          tx["sig"], tx["signer_pubkey"]):
                return {"ok": False, "total": total, "valid": total - 1, "signed": signed_count,
                        "broken_at": tx["id"], "reason": "signature invalid"}
            signed_count += 1

        expected_prev = tx["hash"]

    return {"ok": True, "total": total, "valid": total, "signed": signed_count,
            "broken_at": None}


# ================================
# Core operations
# ================================
def get_balance(agent: str) -> float:
    """Return the current balance for an agent."""
    with _lock:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT balance FROM balances WHERE agent = ?", (agent,))
        row = cur.fetchone()
        conn.close()
        return float(row[0]) if row else 0.0


def get_all_balances() -> dict:
    """Return all agent balances as {agent: balance}."""
    with _lock:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT agent, balance FROM balances ORDER BY balance DESC")
        rows = cur.fetchall()
        conn.close()
        return {agent: float(balance) for agent, balance in rows}


# Actions that are exempt from the daily cap (topups, internal accounting moves)
DAILY_CAP_EXEMPT_ACTIONS = {
    "topup",            # human buys OZC from store
    "auto.topup",       # automatic refill
    "auction.win",      # internal economy plumbing
    "task.assign",
    "task.report",
}


def transfer(
    from_agent: str,
    to_agent: str,
    amount: float,
    action: str,
    detail: str = "",
) -> dict:
    """
    Move OZC from one agent to another and log to the ledger.

    Returns the transaction record on success.
    Raises ValueError on insufficient balance or invalid agents.
    """
    if amount < 0:
        raise ValueError("amount must be non-negative")
    if from_agent == to_agent:
        raise ValueError("from_agent and to_agent must differ")

    with _lock:
        conn = _conn()
        cur = conn.cursor()

        # Make sure both agents exist
        for a in (from_agent, to_agent):
            cur.execute("SELECT balance FROM balances WHERE agent = ?", (a,))
            if cur.fetchone() is None:
                # Auto-create with zero balance
                cur.execute(
                    "INSERT INTO balances (agent, balance) VALUES (?, ?)", (a, 0)
                )

        # Check daily AND monthly caps (both exempt internal moves and topups)
        if action not in DAILY_CAP_EXEMPT_ACTIONS:
            today_start = _today_start_ts()
            month_start = _month_start_ts()
            exempt_clause = (
                "action NOT IN ('topup','auto.topup','auction.win','task.assign','task.report')"
            )
            cur.execute(
                f"SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE ts >= ? AND {exempt_clause}",
                (today_start,),
            )
            spent_today = float(cur.fetchone()[0])
            if spent_today + amount > DAILY_BUDGET_CAP:
                conn.close()
                raise ValueError(
                    f"daily budget cap exceeded: spent={spent_today} + {amount} > {DAILY_BUDGET_CAP}"
                )

            cur.execute(
                f"SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE ts >= ? AND {exempt_clause}",
                (month_start,),
            )
            spent_month = float(cur.fetchone()[0])
            if ozc_to_jpy(spent_month + amount) > MONTHLY_REAL_CAP_JPY:
                conn.close()
                raise ValueError(
                    f"monthly real-money cap exceeded: ¥{ozc_to_jpy(spent_month + amount):.0f} > ¥{MONTHLY_REAL_CAP_JPY}"
                )

        # Verify the most recent block hasn't been tampered with before
        # appending. If the chain is broken, refuse to write — this stops
        # an attacker who edited the DB from extending it cleanly.
        # NOTE: must fetch signer_pubkey + sig too, otherwise signed blocks
        # (Phase 2+) re-hash to the unsigned schema and the check spuriously
        # fails.
        cur.execute(
            "SELECT id, ts, from_agent, to_agent, amount, action, detail, "
            "       prev_hash, hash, signer_pubkey, sig "
            "FROM ledger ORDER BY id DESC LIMIT 1"
        )
        last = cur.fetchone()
        if last is not None:
            last_tx = {
                "id": last[0], "ts": last[1], "from_agent": last[2],
                "to_agent": last[3], "amount": last[4], "action": last[5],
                "detail": last[6], "prev_hash": last[7],
                "signer_pubkey": last[9], "sig": last[10],
            }
            if _compute_block_hash(last_tx) != last[8]:
                conn.close()
                raise ValueError(
                    f"ledger integrity check failed at block #{last[0]} — refusing to write"
                )

        # Check from balance
        cur.execute("SELECT balance FROM balances WHERE agent = ?", (from_agent,))
        from_balance = float(cur.fetchone()[0])
        if from_balance < amount:
            conn.close()
            raise ValueError(
                f"insufficient balance: {from_agent} has {from_balance}, need {amount}"
            )

        # Apply changes
        cur.execute(
            "UPDATE balances SET balance = balance - ? WHERE agent = ?",
            (amount, from_agent),
        )
        cur.execute(
            "UPDATE balances SET balance = balance + ? WHERE agent = ?",
            (amount, to_agent),
        )

        # Get post-transfer balances
        cur.execute("SELECT balance FROM balances WHERE agent = ?", (from_agent,))
        from_after = float(cur.fetchone()[0])
        cur.execute("SELECT balance FROM balances WHERE agent = ?", (to_agent,))
        to_after = float(cur.fetchone()[0])

        # Insert ledger row with chained hash
        ts = time.time()
        prev_hash = _get_last_hash(cur)

        # Phase 2: sign the transaction with the local identity if available.
        # Opportunistic — if there's no identity yet, the transfer still works
        # without a signature (pre-Phase-2 compat mode).
        signer_pubkey = None
        sig_hex = None
        try:
            from ozc import identity as oz_identity
            if oz_identity.has_identity():
                signer_pubkey = oz_identity.public_key_hex()
        except ImportError:
            pass

        cur.execute(
            """INSERT INTO ledger
               (ts, from_agent, to_agent, amount, action, detail,
                from_balance_after, to_balance_after,
                prev_hash, hash, signer_pubkey, sig)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts, from_agent, to_agent, amount, action, detail,
             from_after, to_after, prev_hash, "", signer_pubkey, None),
        )
        tx_id = cur.lastrowid

        # Build the tx payload for signing. The signature covers the canonical
        # serialization of (id, ts, from, to, amount, action, detail, prev,
        # signer) — i.e. everything that identifies this transaction uniquely.
        tx_for_hash = {
            "id": tx_id,
            "ts": ts,
            "from_agent": from_agent,
            "to_agent": to_agent,
            "amount": amount,
            "action": action,
            "detail": detail,
            "prev_hash": prev_hash,
            "signer_pubkey": signer_pubkey,
            "sig": None,
        }

        # Sign the unsigned-form block hash (without the sig field)
        if signer_pubkey is not None:
            try:
                from ozc import identity as oz_identity
                # Serialize the same payload that will go into the block hash
                # but WITHOUT the sig. Sign it with the local private key.
                signing_payload = _compute_block_hash(tx_for_hash)
                sig_hex = oz_identity.sign_hex(signing_payload.encode("utf-8"))
                tx_for_hash["sig"] = sig_hex
                cur.execute("UPDATE ledger SET sig = ? WHERE id = ?", (sig_hex, tx_id))
            except Exception as e:
                # Signing is opportunistic — log and continue unsigned
                print(f"  [economy] signing failed: {e}")
                signer_pubkey = None
                tx_for_hash["signer_pubkey"] = None
                tx_for_hash["sig"] = None
                cur.execute("UPDATE ledger SET signer_pubkey = NULL WHERE id = ?", (tx_id,))

        # Now compute the final block hash (with sig committed, if any)
        block_hash = _compute_block_hash(tx_for_hash)
        cur.execute("UPDATE ledger SET hash = ? WHERE id = ?", (block_hash, tx_id))
        conn.close()

        return {
            "id": tx_id,
            "ts": ts,
            "from_agent": from_agent,
            "to_agent": to_agent,
            "amount": amount,
            "action": action,
            "detail": detail,
            "from_balance_after": from_after,
            "to_balance_after": to_after,
            "prev_hash": prev_hash,
            "hash": block_hash,
            "signer_pubkey": signer_pubkey,
            "sig": sig_hex,
        }


def charge_action(agent: str, action: str, detail: str = "") -> dict:
    """
    Charge an agent for performing a known action.
    Funds flow from the agent → treasury (cost of resources).
    """
    if action not in PRICE_TABLE:
        raise ValueError(f"unknown action: {action}")
    cost = PRICE_TABLE[action]
    return transfer(agent, "treasury", cost, action, detail)


def assign_task(
    from_agent: str, to_agent: str, amount: float, detail: str = ""
) -> dict:
    """
    Assign a task with a budget. Funds flow from supervisor → worker.
    Detail typically describes the task.
    """
    return transfer(from_agent, to_agent, amount, "task.assign", detail)


def report_completion(
    from_agent: str, to_agent: str, amount: float, detail: str = ""
) -> dict:
    """Worker reports back to supervisor and returns unused budget."""
    return transfer(from_agent, to_agent, amount, "task.report", detail)


def get_ledger(limit: int = 50, since_ts: Optional[float] = None) -> list:
    """Return recent ledger entries (most recent first)."""
    with _lock:
        conn = _conn()
        cur = conn.cursor()
        if since_ts is not None:
            cur.execute(
                "SELECT id, ts, from_agent, to_agent, amount, action, detail, from_balance_after, to_balance_after, prev_hash, hash "
                "FROM ledger WHERE ts > ? ORDER BY ts DESC LIMIT ?",
                (since_ts, limit),
            )
        else:
            cur.execute(
                "SELECT id, ts, from_agent, to_agent, amount, action, detail, from_balance_after, to_balance_after, prev_hash, hash "
                "FROM ledger ORDER BY ts DESC LIMIT ?",
                (limit,),
            )
        rows = cur.fetchall()
        conn.close()

        return [
            {
                "id": r[0],
                "ts": r[1],
                "from_agent": r[2],
                "to_agent": r[3],
                "amount": r[4],
                "action": r[5],
                "detail": r[6],
                "from_balance_after": r[7],
                "to_balance_after": r[8],
                "prev_hash": r[9],
                "hash": r[10],
            }
            for r in rows
        ]


def get_daily_stats() -> dict:
    """Return today's + this month's REAL resource spending (excludes topups + internal moves)."""
    today_start = _today_start_ts()
    month_start = _month_start_ts()
    exempt_clause = "action NOT IN ('topup','auto.topup','auction.win','task.assign','task.report')"
    with _lock:
        conn = _conn()
        cur = conn.cursor()
        cur.execute(
            f"SELECT COALESCE(SUM(amount), 0), COUNT(*) FROM ledger WHERE ts >= ? AND {exempt_clause}",
            (today_start,),
        )
        spent_today, count_today = cur.fetchone()
        cur.execute(
            f"SELECT COALESCE(SUM(amount), 0), COUNT(*) FROM ledger WHERE ts >= ? AND {exempt_clause}",
            (month_start,),
        )
        spent_month, count_month = cur.fetchone()
        conn.close()
        return {
            "spent_today": float(spent_today),
            "spent_today_jpy": ozc_to_jpy(float(spent_today)),
            "spent_month": float(spent_month),
            "spent_month_jpy": ozc_to_jpy(float(spent_month)),
            "transactions_today": int(count_today),
            "transactions_month": int(count_month),
            "daily_cap": DAILY_BUDGET_CAP,
            "daily_cap_jpy": ozc_to_jpy(DAILY_BUDGET_CAP),
            "monthly_cap_jpy": MONTHLY_REAL_CAP_JPY,
            "monthly_remaining_jpy": MONTHLY_REAL_CAP_JPY - ozc_to_jpy(float(spent_month)),
            "remaining": float(DAILY_BUDGET_CAP - spent_today),
            "ozc_to_jpy": OZC_TO_JPY,
        }


def topup(agent: str, amount: float, source: str = "human") -> dict:
    """
    Mint new OZC and credit it to an agent. Used when the human user
    "buys" OZC for themselves or for hitomi.

    Funds flow from `treasury` (the system's mint) to the target agent.
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    return transfer("treasury", agent, amount, "topup", source)


def reset_daily_balances():
    """Reset all balances to their initial state. Called at midnight or on demand."""
    with _lock:
        conn = _conn()
        cur = conn.cursor()
        for agent, balance in INITIAL_BALANCES.items():
            cur.execute(
                "INSERT INTO balances (agent, balance) VALUES (?, ?) "
                "ON CONFLICT(agent) DO UPDATE SET balance = ?",
                (agent, balance, balance),
            )
        conn.close()


def _today_start_ts() -> float:
    """Return the unix timestamp at the start of today (local time)."""
    now = datetime.now()
    start = datetime(now.year, now.month, now.day)
    return start.timestamp()


def _month_start_ts() -> float:
    """Return the unix timestamp at the start of this month (local time)."""
    now = datetime.now()
    start = datetime(now.year, now.month, 1)
    return start.timestamp()


# ================================
# Agent registration & reputation (used by the daemon's /register and
# /reputation routes; also callable directly from Python)
# ================================
def register_agent(agent: str, initial_balance: float = 0) -> dict:
    """Register a new agent. Idempotent: re-registering returns the existing
    balance instead of overwriting.

    If `initial_balance` > 0, that amount is minted from the treasury via a
    proper ledger transfer so chain conservation holds.
    """
    if not agent or not isinstance(agent, str):
        raise ValueError("agent name required")
    agent = agent.strip()
    if len(agent) > 64:
        raise ValueError("agent name too long")
    if initial_balance < 0:
        raise ValueError("initial_balance must be non-negative")

    init_db()
    with _lock:
        conn = _conn()
        cur = conn.cursor()
        cur.execute("SELECT balance FROM balances WHERE agent = ?", (agent,))
        existing = cur.fetchone()
        if existing is not None:
            current = float(existing[0])
            conn.close()
            return {
                "ok": True, "agent": agent, "balance": current,
                "created": False,
            }
        cur.execute(
            "INSERT INTO balances (agent, balance) VALUES (?, ?)",
            (agent, 0),
        )
        conn.close()

    # Mint the starting balance from treasury if requested. Done outside the
    # lock so transfer() can take its own lock.
    if initial_balance > 0:
        try:
            transfer("treasury", agent, initial_balance, "register", agent)
        except ValueError as e:
            # If treasury can't fund it, leave the agent registered with 0
            return {
                "ok": True, "agent": agent, "balance": 0.0, "created": True,
                "warning": f"could not mint {initial_balance}: {e}",
            }
    return {
        "ok": True, "agent": agent,
        "balance": float(initial_balance), "created": True,
    }


def get_reputation(agent: str) -> dict:
    """Compute reputation metrics from the ledger history.

    Returns:
        {
          agent, balance,
          tasks_assigned, tasks_completed, completion_rate,
          total_earned, total_spent, net,
          actions_charged,
          first_seen, last_seen,
          tx_count,
        }
    """
    if not agent:
        raise ValueError("agent required")
    init_db()
    with _lock:
        conn = _conn()
        cur = conn.cursor()

        # Tasks assigned to me / completed by me
        cur.execute(
            "SELECT COUNT(*) FROM ledger WHERE to_agent = ? AND action = 'task.assign'",
            (agent,),
        )
        tasks_assigned = int(cur.fetchone()[0])
        cur.execute(
            "SELECT COUNT(*) FROM ledger WHERE from_agent = ? AND action = 'task.report'",
            (agent,),
        )
        tasks_completed = int(cur.fetchone()[0])

        # Money in / out
        cur.execute("SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE to_agent = ?", (agent,))
        total_earned = float(cur.fetchone()[0])
        cur.execute("SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE from_agent = ?", (agent,))
        total_spent = float(cur.fetchone()[0])

        # Charged LLM/resource calls
        cur.execute(
            "SELECT COUNT(*) FROM ledger WHERE from_agent = ? AND action LIKE 'llm.%'",
            (agent,),
        )
        actions_charged = int(cur.fetchone()[0])

        # Activity window
        cur.execute(
            "SELECT MIN(ts), MAX(ts), COUNT(*) FROM ledger WHERE from_agent = ? OR to_agent = ?",
            (agent, agent),
        )
        first_seen, last_seen, tx_count = cur.fetchone()

        # Current balance
        cur.execute("SELECT balance FROM balances WHERE agent = ?", (agent,))
        bal_row = cur.fetchone()
        balance = float(bal_row[0]) if bal_row else 0.0
        conn.close()

    completion_rate = (tasks_completed / tasks_assigned) if tasks_assigned > 0 else None

    return {
        "agent": agent,
        "balance": balance,
        "tasks_assigned": tasks_assigned,
        "tasks_completed": tasks_completed,
        "completion_rate": completion_rate,
        "total_earned": total_earned,
        "total_spent": total_spent,
        "net": total_earned - total_spent,
        "actions_charged": actions_charged,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "tx_count": int(tx_count or 0),
    }


# ================================
# OZC HTTP API Daemon (`ozc serve`)
# ================================
# A standalone protocol-layer daemon. This is the network face of OZC: any
# client (the OZ 3D viewer, a CLI tool, another agent, a mobile app) talks
# to it over plain HTTP/JSON without needing to know about SQLite, hashing,
# or any of the internal Python.
#
# Design:
# - Stdlib only (http.server) — no FastAPI/Flask dependency
# - ThreadingHTTPServer so concurrent agents don't block each other
# - All operations route through the existing thread-safe functions above,
#   so this file is the single source of truth for OZC state
# - Loopback (127.0.0.1) by default with X-OZ-Token auth, matching the
#   security model used by oz_webserver.py
#
# Endpoints:
#   GET    /status                — daemon health, totals, uptime
#   GET    /balances              — { agent: balance }
#   GET    /balance/<agent>       — single agent balance
#   GET    /ledger?limit=&offset=&since=  — recent transactions
#   GET    /reputation/<agent>    — completion rate, earned/spent, history window
#   POST   /transfer              — { from, to, amount, action?, detail? }
#   POST   /register              — { agent, initial_balance? }
#
# Run with: `python3 oz_economy.py serve --port 8800`
# Or once OZC is consolidated: `ozc serve --port 8800`

OZC_DAEMON_PORT = 8800
_OZC_TOKEN_PATH = os.path.expanduser("~/.openclaw/oz_token")
_OZC_DAEMON_START_TIME: Optional[float] = None


def _load_oz_token() -> Optional[str]:
    try:
        with open(_OZC_TOKEN_PATH, "r") as f:
            t = f.read().strip()
            return t or None
    except (FileNotFoundError, PermissionError):
        return None


class _OZCDaemonHandler(http.server.BaseHTTPRequestHandler):
    """Stdlib HTTP handler for the OZC daemon.

    Auth: X-OZ-Token header (matches oz_webserver.py token model). Set
    `_skip_auth = True` on the server instance to bypass for development.
    """

    # Quieter than the default
    def log_message(self, fmt, *args):
        sys.stderr.write(f"  [ozc] {self.address_string()} {fmt % args}\n")

    # ---- helpers ----
    def _send(self, body: dict, status: int = 200) -> None:
        try:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            payload = b'{"ok":false,"error":"unserializable response"}'
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "X-OZ-Token, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except BrokenPipeError:
            pass

    def _check_auth(self) -> bool:
        if getattr(self.server, "_skip_auth", False):
            return True
        expected = getattr(self.server, "_token", None)
        if not expected:
            self._send({"ok": False, "error": "server has no token configured"}, 500)
            return False
        provided = self.headers.get("X-OZ-Token", "")
        # Constant-time comparison to defeat timing attacks
        if len(provided) != len(expected) or not all(
            a == b for a, b in zip(provided, expected)
        ):
            self._send({"ok": False, "error": "unauthorized"}, 401)
            return False
        return True

    def _read_body(self) -> Optional[dict]:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self._send({"ok": False, "error": "bad content length"}, 400)
            return None
        if length > 1024 * 1024:
            self._send({"ok": False, "error": "request too large"}, 413)
            return None
        if length == 0:
            return {}
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                self._send({"ok": False, "error": "body must be a JSON object"}, 400)
                return None
            return data
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send({"ok": False, "error": "invalid json"}, 400)
            return None

    # ---- CORS preflight ----
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "X-OZ-Token, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    # ---- GET routes ----
    def do_GET(self):
        if not self._check_auth():
            return
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        try:
            if path == "/status":
                self._handle_status()
                return

            if path == "/balances":
                self._send({"ok": True, "balances": get_all_balances()})
                return

            if path.startswith("/balance/"):
                agent = path[len("/balance/"):]
                if not agent:
                    self._send({"ok": False, "error": "agent required"}, 400)
                    return
                self._send({
                    "ok": True, "agent": agent, "balance": get_balance(agent),
                })
                return

            if path == "/ledger":
                self._handle_ledger(qs)
                return

            if path.startswith("/reputation/"):
                agent = path[len("/reputation/"):]
                if not agent:
                    self._send({"ok": False, "error": "agent required"}, 400)
                    return
                self._send({"ok": True, **get_reputation(agent)})
                return

            self._send({"ok": False, "error": f"unknown route: {path}"}, 404)
        except Exception as e:
            sys.stderr.write(f"  [ozc] GET {path} error: {e}\n")
            self._send({"ok": False, "error": "internal error"}, 500)

    def _handle_status(self) -> None:
        uptime = time.time() - (_OZC_DAEMON_START_TIME or time.time())
        with _lock:
            conn = _conn()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM ledger")
            ledger_count = int(cur.fetchone()[0])
            cur.execute("SELECT COUNT(*) FROM balances")
            agent_count = int(cur.fetchone()[0])
            conn.close()
        onchain_available = False
        try:
            from ozc import onchain as _oz_onchain  # noqa: F401
            onchain_available = True
        except ImportError:
            pass
        self._send({
            "ok": True,
            "service": "ozc-daemon",
            "version": "1.0",
            "uptime_seconds": round(uptime, 1),
            "db_path": DB_PATH,
            "agents_registered": agent_count,
            "ledger_blocks": ledger_count,
            "daily_cap_ozc": DAILY_BUDGET_CAP,
            "ozc_to_jpy": OZC_TO_JPY,
            "onchain_bridge": onchain_available,
        })

    def _handle_ledger(self, qs: dict) -> None:
        try:
            limit = int(qs.get("limit", ["50"])[0])
            offset = int(qs.get("offset", ["0"])[0])
            since_raw = qs.get("since", [""])[0]
            since = float(since_raw) if since_raw else None
        except ValueError:
            self._send({"ok": False, "error": "bad query params"}, 400)
            return
        limit = max(1, min(limit, 1000))
        offset = max(0, offset)
        # get_ledger() doesn't support offset directly; fetch limit+offset
        # then slice. Cheap for typical limits (<= 1000).
        rows = get_ledger(limit + offset, since_ts=since)
        page = rows[offset:offset + limit]
        self._send({
            "ok": True,
            "limit": limit,
            "offset": offset,
            "count": len(page),
            "transactions": page,
        })

    # ---- POST routes ----
    def do_POST(self):
        if not self._check_auth():
            return
        from urllib.parse import urlparse
        path = urlparse(self.path).path

        body = self._read_body()
        if body is None:
            return

        try:
            if path == "/transfer":
                self._handle_transfer(body)
                return
            if path == "/register":
                self._handle_register(body)
                return
            self._send({"ok": False, "error": f"unknown route: {path}"}, 404)
        except Exception as e:
            sys.stderr.write(f"  [ozc] POST {path} error: {e}\n")
            self._send({"ok": False, "error": "internal error"}, 500)

    def _handle_transfer(self, body: dict) -> None:
        # Accept both {from, to} and {from_agent, to_agent} for ergonomics
        from_agent = str(body.get("from") or body.get("from_agent") or "").strip()
        to_agent = str(body.get("to") or body.get("to_agent") or "").strip()
        try:
            amount = float(body.get("amount", 0))
        except (TypeError, ValueError):
            self._send({"ok": False, "error": "amount must be numeric"}, 400)
            return
        action = str(body.get("action", "manual"))[:40]
        detail = str(body.get("detail", ""))[:200]
        if not from_agent or not to_agent:
            self._send({"ok": False, "error": "from and to required"}, 400)
            return
        try:
            tx = transfer(from_agent, to_agent, amount, action, detail)
            self._send({"ok": True, "transaction": tx})
        except ValueError as e:
            self._send({"ok": False, "error": str(e)}, 400)

    def _handle_register(self, body: dict) -> None:
        agent = str(body.get("agent", "")).strip()
        try:
            initial = float(body.get("initial_balance", 0))
        except (TypeError, ValueError):
            self._send({"ok": False, "error": "initial_balance must be numeric"}, 400)
            return
        if not agent:
            self._send({"ok": False, "error": "agent required"}, 400)
            return
        try:
            self._send(register_agent(agent, initial))
        except ValueError as e:
            self._send({"ok": False, "error": str(e)}, 400)


class _OZCDaemonServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(port: int = OZC_DAEMON_PORT, bind: str = "127.0.0.1",
          auth: bool = True) -> None:
    """Run the OZC HTTP daemon. Blocks until SIGINT.

    Args:
        port: TCP port to listen on (default 8800)
        bind: bind address (default 127.0.0.1 loopback-only)
        auth: if True, require X-OZ-Token header matching ~/.openclaw/oz_token
    """
    global _OZC_DAEMON_START_TIME
    init_db()
    _OZC_DAEMON_START_TIME = time.time()

    server = _OZCDaemonServer((bind, port), _OZCDaemonHandler)
    server._token = _load_oz_token() if auth else None
    server._skip_auth = not auth

    if auth and not server._token:
        print(f"[ozc] WARNING: auth requested but no token at {_OZC_TOKEN_PATH}")
        print(f"[ozc] Either start oz_webserver.py once to create the token,")
        print(f"[ozc] or restart with --no-auth (development only).")
        server.server_close()
        return

    print(f"[ozc] OZC daemon listening on {bind}:{port}")
    print(f"[ozc] auth: {'token (X-OZ-Token)' if auth else 'DISABLED (--no-auth)'}")
    print(f"[ozc] db:   {DB_PATH}")
    if bind != "127.0.0.1":
        print(f"[ozc] WARNING: non-loopback bind ({bind}). Token is the only protection.")
    sys.stdout.flush()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[ozc] shutting down")
    finally:
        server.shutdown()
        server.server_close()


# ================================
# CLI for testing
# ================================
def main():
    import argparse

    parser = argparse.ArgumentParser(description="OZ Economy CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Initialize the database")
    sub.add_parser("balances", help="Show all balances")
    sub.add_parser("ledger", help="Show recent transactions")
    sub.add_parser("stats", help="Show today's spending stats")
    sub.add_parser("reset", help="Reset balances to initial state")
    sub.add_parser("verify", help="Verify the entire ledger chain")
    sub.add_parser("onchain", help="Show real OZC on-chain supply + mapped wallets (read-only)")

    serve_p = sub.add_parser("serve", help="Run the OZC HTTP API daemon")
    serve_p.add_argument("--port", type=int, default=OZC_DAEMON_PORT,
                         help=f"port (default {OZC_DAEMON_PORT})")
    serve_p.add_argument("--bind", default="127.0.0.1",
                         help="bind address (default 127.0.0.1 loopback-only)")
    serve_p.add_argument("--no-auth", action="store_true",
                         help="disable token auth (development only)")

    register_p = sub.add_parser("register", help="Register a new agent")
    register_p.add_argument("agent")
    register_p.add_argument("--initial", type=float, default=0,
                            help="starting balance to mint from treasury")

    rep_p = sub.add_parser("reputation", help="Show an agent's reputation metrics")
    rep_p.add_argument("agent")

    transfer_p = sub.add_parser("transfer", help="Transfer OZC")
    transfer_p.add_argument("from_agent")
    transfer_p.add_argument("to_agent")
    transfer_p.add_argument("amount", type=float)
    transfer_p.add_argument("--action", default="manual")
    transfer_p.add_argument("--detail", default="")

    charge_p = sub.add_parser("charge", help="Charge an agent for a known action")
    charge_p.add_argument("agent")
    charge_p.add_argument("action", choices=list(PRICE_TABLE.keys()))
    charge_p.add_argument("--detail", default="")

    args = parser.parse_args()
    init_db()

    if args.cmd == "init":
        print(f"Initialized {DB_PATH}")
    elif args.cmd == "balances":
        for agent, balance in get_all_balances().items():
            print(f"  {agent:12} {balance:>12.2f} OZC")
    elif args.cmd == "ledger":
        for tx in get_ledger(20):
            ts = datetime.fromtimestamp(tx["ts"]).strftime("%H:%M:%S")
            print(
                f"  [{ts}] {tx['from_agent']:10} -> {tx['to_agent']:10} "
                f"{tx['amount']:>8.2f} OZC  {tx['action']:18} {tx['detail']}"
            )
    elif args.cmd == "stats":
        s = get_daily_stats()
        print(json.dumps(s, indent=2, ensure_ascii=False))
    elif args.cmd == "reset":
        reset_daily_balances()
        print("Reset complete")
    elif args.cmd == "verify":
        result = verify_chain()
        print(json.dumps(result, indent=2))
    elif args.cmd == "onchain":
        # Read-only bridge to the real OZC SPL token on Solana. This is
        # intentionally lazy-imported: if ozc.onchain is removed or
        # httpx is unavailable, the rest of this CLI keeps working.
        try:
            from ozc import onchain as oz_onchain
        except ImportError as e:
            print(f"on-chain bridge unavailable: {e}")
            return
        try:
            supply = oz_onchain.get_ozc_total_supply()
        except oz_onchain.OnchainError as e:
            print(f"RPC failed: {e}")
            return
        print(f"OZC mint:     {oz_onchain.OZC_MINT}")
        print(f"Total supply: {supply['amount']:,.{supply['decimals']}f} OZC on-chain")
        print()
        wallets = oz_onchain.load_wallets().get("wallets", {})
        if not wallets:
            print("(no agent wallets mapped in oz_wallets.json)")
            return
        print(f"Agent wallets ({len(wallets)}):")
        for agent, info in wallets.items():
            addr = info.get("address", "?")
            role = info.get("role", "")
            try:
                bal = oz_onchain.get_ozc_balance(addr)
                ozc_str = f"{bal['amount']:,.{bal['decimals']}f} OZC"
            except oz_onchain.OnchainError as e:
                ozc_str = f"(rpc error: {e})"
            print(f"  {agent:12} [{role:10}] {ozc_str}")
            print(f"               {addr}")
    elif args.cmd == "transfer":
        tx = transfer(args.from_agent, args.to_agent, args.amount, args.action, args.detail)
        print(json.dumps(tx, indent=2, ensure_ascii=False))
    elif args.cmd == "charge":
        tx = charge_action(args.agent, args.action, args.detail)
        print(json.dumps(tx, indent=2, ensure_ascii=False))
    elif args.cmd == "serve":
        serve(port=args.port, bind=args.bind, auth=not args.no_auth)
    elif args.cmd == "register":
        result = register_agent(args.agent, args.initial)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.cmd == "reputation":
        result = get_reputation(args.agent)
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

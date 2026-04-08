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
import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from typing import Optional

DB_PATH = os.path.expanduser("~/.openclaw/workspace/oz_economy.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

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
    "human": 100000,        # ユーザー本人 (仮想口座)
    "treasury": 1000000,    # システム口座 (鋳造元)
}

# 1日の総消費上限 (全エージェント合算)
DAILY_BUDGET_CAP = 5000


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
                hash TEXT
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_ledger_ts ON ledger(ts DESC)
        """)
        # Add hash columns if upgrading from older schema
        for col in ("prev_hash", "hash"):
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


# ================================
# Block hashing — tamper-proof ledger
# ================================
GENESIS_HASH = "0" * 64


def _compute_block_hash(tx: dict) -> str:
    """Hash a transaction record together with the previous block's hash."""
    payload = json.dumps(
        {
            "id": tx["id"],
            "ts": round(tx["ts"], 6),
            "from": tx["from_agent"],
            "to": tx["to_agent"],
            "amount": tx["amount"],
            "action": tx["action"],
            "detail": tx["detail"] or "",
            "prev": tx["prev_hash"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _get_last_hash(cur) -> str:
    cur.execute("SELECT hash FROM ledger WHERE hash IS NOT NULL ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    return row[0] if row and row[0] else GENESIS_HASH


def verify_chain() -> dict:
    """
    Walk the entire ledger and verify every block's hash matches its content
    and links correctly to the previous block.

    Returns:
        {ok: bool, total: int, valid: int, broken_at: int|null}
    """
    with _lock:
        conn = _conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, ts, from_agent, to_agent, amount, action, detail, prev_hash, hash "
            "FROM ledger ORDER BY id ASC"
        )
        rows = cur.fetchall()
        conn.close()

    expected_prev = GENESIS_HASH
    total = 0
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
        }
        if tx["prev_hash"] != expected_prev:
            return {"ok": False, "total": total, "valid": total - 1, "broken_at": tx["id"], "reason": "prev_hash mismatch"}
        expected_hash = _compute_block_hash(tx)
        if tx["hash"] != expected_hash:
            return {"ok": False, "total": total, "valid": total - 1, "broken_at": tx["id"], "reason": "hash mismatch"}
        expected_prev = tx["hash"]

    return {"ok": True, "total": total, "valid": total, "broken_at": None}


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

        # Check daily cap
        today_start = _today_start_ts()
        cur.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM ledger WHERE ts >= ?",
            (today_start,),
        )
        spent_today = float(cur.fetchone()[0])
        if spent_today + amount > DAILY_BUDGET_CAP:
            conn.close()
            raise ValueError(
                f"daily budget cap exceeded: spent={spent_today} + {amount} > {DAILY_BUDGET_CAP}"
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
        cur.execute(
            """INSERT INTO ledger
               (ts, from_agent, to_agent, amount, action, detail, from_balance_after, to_balance_after, prev_hash, hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts, from_agent, to_agent, amount, action, detail, from_after, to_after, prev_hash, ""),
        )
        tx_id = cur.lastrowid

        # Now compute the hash for this row using its assigned id
        tx_for_hash = {
            "id": tx_id,
            "ts": ts,
            "from_agent": from_agent,
            "to_agent": to_agent,
            "amount": amount,
            "action": action,
            "detail": detail,
            "prev_hash": prev_hash,
        }
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
    """Return today's spending stats."""
    today_start = _today_start_ts()
    with _lock:
        conn = _conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT COALESCE(SUM(amount), 0), COUNT(*) FROM ledger WHERE ts >= ?",
            (today_start,),
        )
        spent, count = cur.fetchone()
        conn.close()
        return {
            "spent_today": float(spent),
            "transactions_today": int(count),
            "daily_cap": DAILY_BUDGET_CAP,
            "remaining": float(DAILY_BUDGET_CAP - spent),
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
    elif args.cmd == "transfer":
        tx = transfer(args.from_agent, args.to_agent, args.amount, args.action, args.detail)
        print(json.dumps(tx, indent=2, ensure_ascii=False))
    elif args.cmd == "charge":
        tx = charge_action(args.agent, args.action, args.detail)
        print(json.dumps(tx, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

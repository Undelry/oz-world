"""
oz_approvals.py — ユーザー承認キュー

エージェントが "user-approve" 権限のアクションを実行しようとすると:
1. ここに pending request として登録
2. ブラウザの承認パネルに表示
3. ユーザーが Approve / Deny を押す
4. 待機していた呼び出し側がそれを受けて続行

すべての承認・却下は audit log として記録され、
ブロックチェーン台帳に永久保存される (oz_economy.py 経由)。

ストレージはメモリのみ。プロセスが死ねば全てのpendingは消える
(これは安全側のデフォルト — 残しておくとプロセス再起動時に
承認待ちが残って意図しない実行が起きうる)。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Optional


class ApprovalRequest:
    __slots__ = (
        "id", "agent", "action", "detail", "created_at",
        "decided_at", "decision", "_event",
    )

    def __init__(self, agent: str, action: str, detail: str):
        self.id = str(uuid.uuid4())
        self.agent = agent
        self.action = action
        self.detail = detail
        self.created_at = time.time()
        self.decided_at: Optional[float] = None
        self.decision: Optional[str] = None  # "approve" / "deny" / "timeout"
        self._event = threading.Event()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent": self.agent,
            "action": self.action,
            "detail": self.detail,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "decision": self.decision,
        }

    def wait_for_decision(self, timeout: float) -> str:
        """Block until the user decides or the timeout fires."""
        if self._event.wait(timeout):
            return self.decision or "deny"
        # Timeout: deny by default and record it
        if self.decision is None:
            self.decision = "timeout"
            self.decided_at = time.time()
            self._event.set()
        return self.decision

    def resolve(self, decision: str) -> bool:
        """Called by the UI handler. Returns False if already decided."""
        if self.decision is not None:
            return False
        self.decision = decision
        self.decided_at = time.time()
        self._event.set()
        return True


class ApprovalQueue:
    """
    In-memory queue of approval requests, indexed by id.
    Thread-safe. Old decided requests are kept for a short window so the
    UI can render their final state, then garbage-collected.
    """

    def __init__(self, retention_seconds: float = 60.0):
        self._lock = threading.Lock()
        self._items: dict[str, ApprovalRequest] = {}
        self._retention = retention_seconds

    def submit(self, agent: str, action: str, detail: str) -> ApprovalRequest:
        req = ApprovalRequest(agent, action, detail)
        with self._lock:
            self._items[req.id] = req
            self._gc_locked()
        return req

    def resolve(self, request_id: str, decision: str) -> bool:
        with self._lock:
            req = self._items.get(request_id)
        if req is None:
            return False
        if decision not in ("approve", "deny"):
            return False
        return req.resolve(decision)

    def list_pending(self) -> list[dict]:
        with self._lock:
            self._gc_locked()
            return [
                r.to_dict() for r in self._items.values() if r.decision is None
            ]

    def list_recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            self._gc_locked()
            items = sorted(
                self._items.values(),
                key=lambda r: r.decided_at or r.created_at,
                reverse=True,
            )
            return [r.to_dict() for r in items[:limit]]

    def _gc_locked(self):
        """Remove items decided more than `retention_seconds` ago."""
        now = time.time()
        stale = [
            req_id for req_id, r in self._items.items()
            if r.decided_at is not None and (now - r.decided_at) > self._retention
        ]
        for req_id in stale:
            self._items.pop(req_id, None)


# Process-wide singleton
_queue = ApprovalQueue()


def submit(agent: str, action: str, detail: str) -> ApprovalRequest:
    return _queue.submit(agent, action, detail)


def resolve(request_id: str, decision: str) -> bool:
    return _queue.resolve(request_id, decision)


def list_pending() -> list[dict]:
    return _queue.list_pending()


def list_recent(limit: int = 20) -> list[dict]:
    return _queue.list_recent(limit)

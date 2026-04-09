"""
oz_sessions.py — リアルタイムセッション可視化レジストリ

実際に動いている Claude Code セッションを追跡し、フロントエンドに公開する。
これにより OZ の3Dアバター = 本物の AI 稼働 になる (嘘の演出ではなく)。

ライフサイクル:
1. agent.ask が呼ばれた瞬間 → register(session_id, agent, prompt_summary)
2. 結果が返ったら → mark_done(session_id, reply_summary, cost_ozc)
3. mark_done から 30秒後 → 自動的にレジストリから消える (TTL)

レジストリは in-memory dict (プロセス再起動で消える)。
将来は sqlite 永続化で履歴ビューも作れる。

スレッドセーフ: Lock で保護。複数の同時セッションに対応。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Optional

# Lifecycle phases for the 3D animation
PHASE_STARTING = "starting"   # spawning, fade-in
PHASE_WORKING  = "working"    # actively running
PHASE_DONE     = "done"       # completed, showing result
PHASE_IDLE     = "idle"       # finished, TTL countdown
PHASE_GONE     = "gone"       # marked for removal

# How long after done before removal
DONE_TTL_SECONDS = 30


class Session:
    __slots__ = (
        "id", "agent", "prompt", "reply", "cost_ozc",
        "phase", "started_at", "ended_at",
    )

    def __init__(self, agent: str, prompt: str):
        self.id = str(uuid.uuid4())[:12]
        self.agent = agent
        self.prompt = prompt[:200]  # cap for safety
        self.reply: Optional[str] = None
        self.cost_ozc: float = 0.0
        self.phase = PHASE_STARTING
        self.started_at = time.time()
        self.ended_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent": self.agent,
            "prompt": self.prompt,
            "reply": (self.reply or "")[:200],
            "cost_ozc": self.cost_ozc,
            "phase": self.phase,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "age_s": round(time.time() - self.started_at, 1),
        }


class SessionRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}

    def register(self, agent: str, prompt: str) -> Session:
        sess = Session(agent, prompt)
        with self._lock:
            self._sessions[sess.id] = sess
            self._gc_locked()
        return sess

    def mark_working(self, session_id: str):
        with self._lock:
            s = self._sessions.get(session_id)
            if s and s.phase == PHASE_STARTING:
                s.phase = PHASE_WORKING

    def mark_done(self, session_id: str, reply: str = "", cost_ozc: float = 0):
        with self._lock:
            s = self._sessions.get(session_id)
            if s:
                s.phase = PHASE_DONE
                s.reply = reply[:200]
                s.cost_ozc = cost_ozc
                s.ended_at = time.time()

    def mark_failed(self, session_id: str, error: str = ""):
        with self._lock:
            s = self._sessions.get(session_id)
            if s:
                s.phase = PHASE_DONE
                s.reply = f"ERROR: {error[:160]}"
                s.ended_at = time.time()

    def list_active(self) -> list[dict]:
        with self._lock:
            self._gc_locked()
            return [s.to_dict() for s in self._sessions.values()]

    def stats(self) -> dict:
        with self._lock:
            self._gc_locked()
            phases = {}
            for s in self._sessions.values():
                phases[s.phase] = phases.get(s.phase, 0) + 1
            return {
                "total": len(self._sessions),
                "by_phase": phases,
            }

    def _gc_locked(self):
        """Move done sessions to idle, then remove them after TTL."""
        now = time.time()
        to_remove = []
        for sid, s in self._sessions.items():
            if s.phase == PHASE_DONE and s.ended_at:
                if now - s.ended_at > 5:
                    s.phase = PHASE_IDLE
            if s.phase == PHASE_IDLE and s.ended_at:
                if now - s.ended_at > DONE_TTL_SECONDS:
                    to_remove.append(sid)
        for sid in to_remove:
            self._sessions.pop(sid, None)


# Process-wide singleton
_registry = SessionRegistry()


def register(agent: str, prompt: str) -> Session:
    return _registry.register(agent, prompt)


def mark_working(session_id: str):
    _registry.mark_working(session_id)


def mark_done(session_id: str, reply: str = "", cost_ozc: float = 0):
    _registry.mark_done(session_id, reply, cost_ozc)


def mark_failed(session_id: str, error: str = ""):
    _registry.mark_failed(session_id, error)


def list_active() -> list[dict]:
    return _registry.list_active()


def stats() -> dict:
    return _registry.stats()


# CLI for inspection
def main():
    import argparse, json
    parser = argparse.ArgumentParser(description="OZ session registry inspector")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("stats")
    args = parser.parse_args()

    if args.cmd == "list":
        for s in list_active():
            print(json.dumps(s, indent=2, ensure_ascii=False))
    elif args.cmd == "stats":
        print(json.dumps(stats(), indent=2))


if __name__ == "__main__":
    main()

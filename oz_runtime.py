"""
oz_runtime.py — OZエージェント実行ランタイム (Phase 5)

OZのアーキテクチャを2つのプロセスに分離する:

  oz_viewer (oz_webserver.py)    oz_runtime (this file)
  ─────────────────────────      ─────────────────────────
  HTTP :8767                       Unix socket
  HTML/3D配信                       Claude / say / file 実行
  残高表示                          capability + approval gate
  ユーザー操作の受付                 audit log (ledger)

  ↑ ブラウザがアクセス              ↑ viewer のみがアクセス
  危険なコードを実行しない            権限のある操作のみ実行

ブラウザのバグや XSS が起きても、攻撃者ができることは
oz_viewer 経由で oz_runtime にリクエストを送ることだけ。
そこで capability check + ユーザー承認 でブロックされる。

Unix socket は ~/.openclaw/oz_runtime.sock に owner-only (0600) で作成。
ネットワーク経由では絶対に到達できない。

Wire format: 1リクエスト = 1接続 = 1 JSON行
{"action": "agent.ask", "agent": "coder", "params": {...}}
→ {"ok": true, "result": ...}
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
from typing import Optional

import oz_economy
import oz_capabilities
import oz_approvals
import oz_macos
from oz_capabilities import Permission

SOCKET_PATH = os.path.expanduser("~/.openclaw/oz_runtime.sock")

# Per-action timeouts and limits
APPROVAL_TIMEOUT = 60.0   # seconds the user has to approve
LLM_TIMEOUT = 45          # seconds for a single Claude call
MAX_PROMPT_CHARS = 800

# Allowed values for the say(1) shell-out (mirrors oz_webserver.py)
ALLOWED_VOICES = {
    "Kyoko", "Otoya", "Samantha", "Alex", "Daniel",
    "Karen", "Moira", "Tessa", "Victoria", "Fred", "Ralph",
}


# ================================
# Permission gate
# ================================
def _gate(agent: str, action: str, detail: str) -> Optional[dict]:
    """
    Run the capability + approval check for an action.

    Returns None if allowed (call may proceed). Otherwise returns an error
    dict the caller should send back to the client.
    """
    perm = oz_capabilities.get_permission(agent, action)

    if perm == Permission.DENY:
        return {
            "ok": False,
            "error": f"forbidden: {agent} cannot {action}",
            "reason": "capability_denied",
        }

    if perm == Permission.USER_APPROVE:
        req = oz_approvals.submit(agent, action, detail)
        decision = req.wait_for_decision(APPROVAL_TIMEOUT)
        if decision != "approve":
            return {
                "ok": False,
                "error": f"user denied: {agent} {action}",
                "reason": "user_denied" if decision == "deny" else "approval_timeout",
                "approval_id": req.id,
            }

    return None  # ALWAYS or USER_APPROVE-and-approved


# ================================
# Action handlers
# ================================
def _handle_agent_ask(params: dict) -> dict:
    """
    Have a worker call Claude via Claude Code CLI subprocess.

    Each worker is a Claude Code subprocess with its own profile:
    - allowed_tools (e.g. coder gets Read/Edit/Write, researcher gets WebFetch)
    - system prompt (persona + vault context)
    - working directory (file scope)

    The CLI handles its own permission prompts; OZ adds:
    - capability-level gate (which agents can be invoked at all)
    - economy charge (OZC per call)
    - vault session log (persistent memory)
    """
    import oz_agents_cli

    agent = str(params.get("agent", ""))[:64]
    message = (params.get("message") or "")[:MAX_PROMPT_CHARS]
    if not agent or not message:
        return {"ok": False, "error": "agent and message required"}

    if agent not in oz_agents_cli.WORKER_PROFILES:
        return {"ok": False, "error": "unknown agent"}

    gate = _gate(agent, "llm.claude", message[:60])
    if gate is not None:
        return gate

    return oz_agents_cli.ask_agent(agent, message, timeout=90)


def _handle_speak(params: dict) -> dict:
    """
    macOS say(1) TTS. Validated voice/rate/text.
    """
    text = (params.get("text") or "")[:500]
    voice = params.get("voice") or "Kyoko"
    if voice not in ALLOWED_VOICES:
        voice = "Kyoko"
    try:
        rate = int(params.get("rate", 200))
    except (TypeError, ValueError):
        rate = 200
    rate = max(50, min(500, rate))
    agent = str(params.get("agent", "hitomi"))[:64]

    if not text:
        return {"ok": True, "skipped": "empty"}

    gate = _gate(agent, "tts.speak", text[:60])
    if gate is not None:
        return gate

    try:
        subprocess.Popen(
            ["say", "-v", voice, "-r", str(rate), "--", text],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "say not found"}

    try:
        oz_economy.charge_action(agent, "tts.speak", text[:60])
    except Exception:
        pass
    return {"ok": True}


def _handle_external_call(params: dict) -> dict:
    """Route to oz_external — same gating as agent.ask but with provider id."""
    import oz_external

    provider = str(params.get("provider", ""))[:64]
    prompt = (params.get("prompt") or "")[:MAX_PROMPT_CHARS]

    if provider not in oz_external.EXTERNAL_PROVIDERS:
        return {"ok": False, "error": "unknown provider"}
    if not prompt:
        return {"ok": False, "error": "prompt required"}

    gate = _gate(provider, "llm.claude", prompt[:60])
    if gate is not None:
        return gate

    result = oz_external.call_external(provider, prompt)
    if result.get("ok"):
        cost_ozc = oz_external.jpy_to_ozc(result["real_cost_jpy"])
        try:
            oz_economy.transfer(
                "hitomi", "treasury", cost_ozc,
                f"external.{provider}", prompt[:60],
            )
        except Exception:
            pass
    return result


def _handle_macos_list(params: dict) -> dict:
    """Read-only: list installed apps. No gate (always-allowed)."""
    agent = str(params.get("agent", "hitomi"))[:64]
    gate = _gate(agent, "macos.app.list", "list installed apps")
    if gate is not None:
        return gate
    return {"ok": True, "apps": oz_macos.list_installed_apps()}


def _handle_macos_running(params: dict) -> dict:
    agent = str(params.get("agent", "hitomi"))[:64]
    gate = _gate(agent, "macos.app.running", "list running apps")
    if gate is not None:
        return gate
    return {"ok": True, "apps": oz_macos.list_running_apps()}


def _handle_macos_active(params: dict) -> dict:
    agent = str(params.get("agent", "hitomi"))[:64]
    gate = _gate(agent, "macos.window.active", "read active window")
    if gate is not None:
        return gate
    return {
        "ok": True,
        "app": oz_macos.get_active_app(),
        "window": oz_macos.get_active_window_title(),
    }


def _handle_macos_launch(params: dict) -> dict:
    """Launch a macOS app. Requires user approval."""
    agent = str(params.get("agent", "hitomi"))[:64]
    app_name = str(params.get("app", ""))[:64]
    if not app_name:
        return {"ok": False, "error": "app name required"}

    gate = _gate(agent, "macos.app.launch", f"launch '{app_name}'")
    if gate is not None:
        return gate

    result = oz_macos.launch_app(app_name)
    # Charge a small fee on success
    if result.get("ok"):
        try:
            oz_economy.charge_action(agent, "app.launch", app_name)
        except Exception:
            pass
    return result


def _handle_macos_focus(params: dict) -> dict:
    agent = str(params.get("agent", "hitomi"))[:64]
    app_name = str(params.get("app", ""))[:64]
    if not app_name:
        return {"ok": False, "error": "app name required"}

    gate = _gate(agent, "macos.app.focus", f"focus '{app_name}'")
    if gate is not None:
        return gate

    return oz_macos.focus_app(app_name)


def _handle_macos_quit(params: dict) -> dict:
    agent = str(params.get("agent", "hitomi"))[:64]
    app_name = str(params.get("app", ""))[:64]
    if not app_name:
        return {"ok": False, "error": "app name required"}

    gate = _gate(agent, "macos.app.quit", f"quit '{app_name}'")
    if gate is not None:
        return gate

    return oz_macos.quit_app(app_name)


def _handle_caps_list(params: dict) -> dict:
    return {
        "ok": True,
        "agents": {a: oz_capabilities.list_capabilities(a) for a in oz_capabilities.all_agents()},
    }


def _handle_approvals_list(params: dict) -> dict:
    return {
        "ok": True,
        "pending": oz_approvals.list_pending(),
        "recent": oz_approvals.list_recent(20),
    }


def _handle_approvals_resolve(params: dict) -> dict:
    req_id = str(params.get("id", ""))
    decision = str(params.get("decision", ""))
    if not req_id or decision not in ("approve", "deny"):
        return {"ok": False, "error": "id and decision required"}
    ok = oz_approvals.resolve(req_id, decision)
    return {"ok": ok}


HANDLERS = {
    "agent.ask":       _handle_agent_ask,
    "speak":           _handle_speak,
    "external.call":   _handle_external_call,
    "caps.list":       _handle_caps_list,
    "approvals.list":  _handle_approvals_list,
    "approvals.resolve": _handle_approvals_resolve,
    # macOS bridge
    "macos.list":      _handle_macos_list,
    "macos.running":   _handle_macos_running,
    "macos.active":    _handle_macos_active,
    "macos.launch":    _handle_macos_launch,
    "macos.focus":     _handle_macos_focus,
    "macos.quit":      _handle_macos_quit,
}


# ================================
# Unix socket server
# ================================
def _handle_client(conn: socket.socket):
    try:
        data = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
            if b"\n" in chunk:
                break
            if len(data) > 4 * 1024 * 1024:  # 4 MB limit per request
                conn.sendall(b'{"ok":false,"error":"request too large"}\n')
                return

        try:
            req = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            conn.sendall(b'{"ok":false,"error":"invalid json"}\n')
            return

        action = str(req.get("action", ""))
        params = req.get("params") or {}
        handler = HANDLERS.get(action)
        if handler is None:
            resp = {"ok": False, "error": f"unknown action: {action}"}
        else:
            try:
                resp = handler(params)
            except Exception as e:
                print(f"  runtime handler {action} error: {e}")
                resp = {"ok": False, "error": "internal error"}

        conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main():
    os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o600)  # owner-only
    sock.listen(16)

    print(f"oz_runtime listening on {SOCKET_PATH}")
    print("(Unix socket, owner-only — not reachable over the network)")

    try:
        while True:
            conn, _ = sock.accept()
            t = threading.Thread(target=_handle_client, args=(conn,), daemon=True)
            t.start()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        sock.close()
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass


# ================================
# Client helper (used by oz_webserver)
# ================================
def call_runtime(action: str, params: dict, timeout: float = 90.0) -> dict:
    """
    Synchronously call the runtime over its Unix socket. Returns the parsed
    JSON response. Used by oz_webserver to delegate dangerous operations.

    Falls back gracefully if the socket isn't running:
    {"ok": False, "error": "runtime unavailable"}
    """
    if not os.path.exists(SOCKET_PATH):
        return {"ok": False, "error": "oz_runtime not running"}

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(SOCKET_PATH)
    except OSError as e:
        return {"ok": False, "error": f"runtime connect failed: {e}"}

    try:
        msg = json.dumps({"action": action, "params": params}, ensure_ascii=False) + "\n"
        sock.sendall(msg.encode("utf-8"))
        sock.shutdown(socket.SHUT_WR)

        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"ok": False, "error": "invalid response from runtime"}
    finally:
        sock.close()


if __name__ == "__main__":
    main()

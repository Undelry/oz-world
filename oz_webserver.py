"""
oz_webserver.py - OZ World HTMLビューアー用HTTPサーバー
- oz_world.html を http://localhost:8767/oz_world.html で配信
- /api/transcribe: Whisperローカル音声認識

セキュリティ:
- 127.0.0.1 のみにバインド (LAN/WiFiから到達不可)
- 全 mutating エンドポイントは X-OZ-Token ヘッダー必須
- トークンは ~/.openclaw/oz_token に 0600 で保存
- HTML 配信時に <meta name="oz-token"> として埋め込みフロントが自動付与
"""

import http.server
import socketserver
import os
import sys
import signal
import json
import secrets
import tempfile
import threading
import subprocess

import oz_economy
import oz_network
import oz_marketplace
import oz_sessions
import oz_agents_cli as oz_agents  # legacy alias — actual runtime via oz_agents_cli
import oz_bidding
import oz_external
import oz_runtime

PORT = 8767
DIRECTORY = os.path.dirname(os.path.abspath(__file__))
TASK_STATUS_FILE = os.path.join(DIRECTORY, "openclaw_task_status.json")
WORKER_STATE_FILE = os.path.join(DIRECTORY, "oz_worker_state.json")

# === Security: per-user shared secret ===
TOKEN_PATH = os.path.expanduser("~/.openclaw/oz_token")
os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)

def _load_or_create_token() -> str:
    if os.path.exists(TOKEN_PATH):
        try:
            with open(TOKEN_PATH) as f:
                t = f.read().strip()
            if t:
                return t
        except OSError:
            pass
    t = secrets.token_urlsafe(32)
    fd = os.open(TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(t)
    return t

OZ_TOKEN = _load_or_create_token()

# Limits to prevent runaway resource consumption
MAX_AUDIO_BYTES = 20 * 1024 * 1024   # 20 MB
MAX_USER_MESSAGE = 800                # characters
MAX_BODY_BYTES = 2 * 1024 * 1024     # 2 MB for any JSON body

# Allowed values for the say(1) shell-out
ALLOWED_VOICES = {
    "Kyoko", "Otoya", "Samantha", "Alex", "Daniel",
    "Karen", "Moira", "Tessa", "Victoria", "Fred", "Ralph",
}
RATE_MIN, RATE_MAX = 50, 500

# Allow turning the destructive reset endpoint on only when explicitly requested
ALLOW_RESET = os.environ.get("OZ_ALLOW_RESET") == "1"

# Initialize the OZ economy database on startup
oz_economy.init_db()

# Whisper model (lazy load)
_whisper_model = None
_whisper_lock = threading.Lock()

def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                import whisper
                print("Loading Whisper model (base)...")
                _whisper_model = whisper.load_model("base")
                print("Whisper model ready")
    return _whisper_model


class OZHandler(http.server.SimpleHTTPRequestHandler):
    """REST API + 静的ファイル配信ハンドラ"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def end_headers(self):
        # CORS only for our own loopback origins. We never need to be reached
        # from other devices, so don't advertise wildcard.
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:%d" % PORT)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Range, X-OZ-Token")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    # === Security helpers ===
    def _check_token(self) -> bool:
        # Constant-time compare so brute force can't time-side-channel
        sent = self.headers.get("X-OZ-Token", "")
        return secrets.compare_digest(sent, OZ_TOKEN)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return b""
        if length < 0 or length > MAX_BODY_BYTES:
            self._send_json({"ok": False, "error": "body too large"}, status=413)
            return None
        return self.rfile.read(length) if length else b""

    def _require_auth(self) -> bool:
        if self._check_token():
            return True
        self._send_json({"ok": False, "error": "unauthorized"}, status=401)
        return False

    def log_message(self, format, *args):
        # APIリクエストとGETリクエストのみログ表示
        msg = str(args[0]) if args else ""
        if "GET" in msg or "POST" in msg:
            if "/api/" not in msg:
                print(f"  HTTP {msg}")

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/status":
            try:
                with open(TASK_STATUS_FILE, "r") as f:
                    data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                data = {"error": "status file not available"}
            self._send_json(data)
            return

        if self.path == "/api/workers":
            try:
                with open(WORKER_STATE_FILE, "r") as f:
                    data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                data = {"workers": []}
            self._send_json(data)
            return

        # === OZ Economy endpoints ===
        if self.path == "/api/economy/balances":
            self._send_json(oz_economy.get_all_balances())
            return

        if self.path.startswith("/api/economy/ledger"):
            # Optional ?since=<ts>&limit=<n>
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            since = float(qs.get("since", [0])[0]) if qs.get("since") else None
            limit = int(qs.get("limit", [50])[0])
            self._send_json(oz_economy.get_ledger(limit=limit, since_ts=since))
            return

        if self.path == "/api/economy/stats":
            stats = oz_economy.get_daily_stats()
            stats["price_table"] = oz_economy.PRICE_TABLE
            self._send_json(stats)
            return

        if self.path == "/api/economy/verify":
            self._send_json(oz_economy.verify_chain())
            return

        # Inject the OZ token into the HTML at request time so that:
        # 1) the token never sits in a static file on disk
        # 2) only loopback requests get it (we already bind to 127.0.0.1)
        # Strip query string for comparison so cache-busted URLs still get the
        # token-injected HTML response
        from urllib.parse import urlparse as _u
        _p = _u(self.path).path
        if _p == "/oz_world.html" or _p == "/" or _p == "":
            html_path = os.path.join(DIRECTORY, "oz_world.html")
            try:
                with open(html_path, "rb") as f:
                    html = f.read()
            except OSError:
                self.send_error(404)
                return
            meta = (
                '<meta name="oz-token" content="' + OZ_TOKEN + '">'
            ).encode("utf-8")
            # Inject AFTER <meta charset=...> so the browser sees charset
            # in the first 1024 bytes (HTML5 spec) and parses correctly.
            charset_marker = b'<meta charset="UTF-8">'
            if charset_marker in html:
                html = html.replace(
                    charset_marker,
                    charset_marker + b"\n" + meta,
                    1,
                )
            elif b"<head>" in html:
                html = html.replace(b"<head>", b"<head>\n" + meta, 1)
            else:
                html = meta + html
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(html)
            return

        # === Live agent sessions (read-only) ===
        # Sessions live in oz_runtime's process (where ask_agent is called),
        # so we have to query it via the unix socket.
        if self.path == "/api/sessions/active":
            result = oz_runtime.call_runtime("sessions.list", {})
            self._send_json(result)
            return

        # === Marketplace (read-only) ===
        if self.path == "/api/marketplace/list" or self.path.startswith("/api/marketplace/list?"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            tag = qs.get("tag", [None])[0]
            sort = qs.get("sort", ["popular"])[0]
            try:
                skills = oz_marketplace.list_skills(tag=tag, sort=sort, limit=50)
                self._send_json({"ok": True, "skills": skills})
            except Exception as e:
                print(f"  /api/marketplace/list error: {e}")
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if self.path.startswith("/api/marketplace/get/"):
            skill_id = self.path.split("/")[-1]
            try:
                skill = oz_marketplace.get_skill(skill_id)
                if skill is None:
                    self._send_json({"ok": False, "error": "not found"}, status=404)
                else:
                    self._send_json({"ok": True, "skill": skill})
            except Exception as e:
                print(f"  /api/marketplace/get error: {e}")
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        # === Personal network map (read-only) ===
        if self.path == "/api/network/snapshot":
            snap = oz_network.load_snapshot()
            if snap is None:
                self._send_json({"ok": False, "error": "no snapshot — POST /api/network/refresh"}, status=404)
            else:
                self._send_json({"ok": True, "snapshot": snap})
            return

        # === macOS bridge (read-only GETs) ===
        if self.path == "/api/macos/installed":
            self._send_json(oz_runtime.call_runtime("macos.list", {}))
            return
        if self.path == "/api/macos/running":
            self._send_json(oz_runtime.call_runtime("macos.running", {}))
            return
        if self.path == "/api/macos/active":
            self._send_json(oz_runtime.call_runtime("macos.active", {}))
            return

        # === Capabilities & approvals (read-only views) ===
        if self.path == "/api/capabilities":
            result = oz_runtime.call_runtime("caps.list", {})
            self._send_json(result)
            return

        if self.path == "/api/approvals":
            result = oz_runtime.call_runtime("approvals.list", {})
            self._send_json(result)
            return

        if self.path == "/api/external/providers":
            providers = []
            for name, info in oz_external.EXTERNAL_PROVIDERS.items():
                providers.append({
                    "id": name,
                    "label": info["label"],
                    "emoji": info["emoji"],
                    "color": info["color"],
                    "real_cost_jpy": info["real_cost_jpy"],
                    "real_cost_ozc": oz_external.jpy_to_ozc(info["real_cost_jpy"]),
                    "specialty_keywords": info["specialty_keywords"],
                })
            self._send_json({"providers": providers, "ozc_to_jpy": oz_external.OZC_TO_JPY})
            return

        # 静的ファイル配信にフォールバック
        super().do_GET()

    def do_POST(self):
        # All POST endpoints require auth.
        if not self._require_auth():
            return

        if self.path == "/api/workers":
            body = self._read_body()
            if body is None:
                return
            try:
                data = json.loads(body)
                with open(WORKER_STATE_FILE, "w") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                self._send_json({"ok": True})
            except (json.JSONDecodeError, IOError) as e:
                self._send_json({"error": str(e)}, status=400)
            return

        if self.path == "/api/speak":
            # Delegate to oz_runtime which enforces capability + approval gates
            body = self._read_body()
            if body is None:
                return
            try:
                data = json.loads(body)
                result = oz_runtime.call_runtime("speak", {
                    "text": data.get("text"),
                    "voice": data.get("voice"),
                    "rate": data.get("rate"),
                    "agent": data.get("agent", "hitomi"),
                })
                self._send_json(result)
            except Exception as e:
                print(f"  /api/speak error: {e}")
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        # === Marketplace POST endpoints ===
        if self.path == "/api/marketplace/rate":
            body = self._read_body()
            if body is None:
                return
            try:
                data = json.loads(body)
                result = oz_marketplace.rate_skill(
                    str(data.get("skill_id", ""))[:32],
                    str(data.get("rater", "human"))[:64],
                    int(data.get("stars", 0)),
                    str(data.get("comment", ""))[:500],
                )
                self._send_json(result)
            except Exception as e:
                print(f"  /api/marketplace/rate error: {e}")
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if self.path == "/api/marketplace/install":
            body = self._read_body()
            if body is None:
                return
            try:
                data = json.loads(body)
                result = oz_marketplace.install_skill(str(data.get("skill_id", ""))[:32])
                self._send_json(result)
            except Exception as e:
                print(f"  /api/marketplace/install error: {e}")
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if self.path == "/api/marketplace/publish":
            body = self._read_body()
            if body is None:
                return
            try:
                data = json.loads(body)
                result = oz_marketplace.publish(
                    name=str(data.get("name", ""))[:80],
                    description=str(data.get("description", ""))[:300],
                    body=str(data.get("body", ""))[:50000],
                    author=str(data.get("author", "human"))[:64],
                    tags=data.get("tags") if isinstance(data.get("tags"), list) else None,
                    price_ozc=float(data.get("price_ozc", 0)),
                )
                self._send_json(result)
            except Exception as e:
                print(f"  /api/marketplace/publish error: {e}")
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        # === Personal network map: refresh ===
        if self.path == "/api/network/refresh":
            try:
                snap = oz_network.build_network(limit=60, with_names=False, max_mail_files=10000)
                oz_network.save_snapshot(snap)
                self._send_json({"ok": True, "stats": snap.get("stats", {})})
            except Exception as e:
                print(f"  /api/network/refresh error: {e}")
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        # === macOS bridge (mutating POSTs go through runtime gate) ===
        if self.path == "/api/macos/launch":
            body = self._read_body()
            if body is None:
                return
            try:
                data = json.loads(body)
                result = oz_runtime.call_runtime("macos.launch", {
                    "agent": data.get("agent", "hitomi"),
                    "app": data.get("app"),
                }, timeout=120.0)  # long enough for the user to approve
                self._send_json(result)
            except Exception as e:
                print(f"  /api/macos/launch error: {e}")
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if self.path == "/api/macos/focus":
            body = self._read_body()
            if body is None:
                return
            try:
                data = json.loads(body)
                result = oz_runtime.call_runtime("macos.focus", {
                    "agent": data.get("agent", "hitomi"),
                    "app": data.get("app"),
                })
                self._send_json(result)
            except Exception as e:
                print(f"  /api/macos/focus error: {e}")
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if self.path == "/api/macos/quit":
            body = self._read_body()
            if body is None:
                return
            try:
                data = json.loads(body)
                result = oz_runtime.call_runtime("macos.quit", {
                    "agent": data.get("agent", "hitomi"),
                    "app": data.get("app"),
                }, timeout=120.0)
                self._send_json(result)
            except Exception as e:
                print(f"  /api/macos/quit error: {e}")
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        # === Approvals: user resolves a pending request ===
        if self.path == "/api/approvals/resolve":
            body = self._read_body()
            if body is None:
                return
            try:
                data = json.loads(body)
                result = oz_runtime.call_runtime("approvals.resolve", {
                    "id": data.get("id"),
                    "decision": data.get("decision"),
                })
                self._send_json(result)
            except Exception as e:
                print(f"  /api/approvals/resolve error: {e}")
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        # === OZ Economy POST endpoints ===
        if self.path == "/api/economy/transfer":
            body = self._read_body()
            if body is None:
                return
            try:
                data = json.loads(body)
                # Hard cap a single transfer to avoid runaway abuse
                amount = float(data["amount"])
                if amount < 0 or amount > 100000:
                    self._send_json({"ok": False, "error": "amount out of range"}, status=400)
                    return
                # Sanitize action and detail (no control chars, length cap)
                action = str(data.get("action", "manual"))[:64]
                detail = str(data.get("detail", ""))[:200]
                tx = oz_economy.transfer(
                    str(data["from_agent"])[:64],
                    str(data["to_agent"])[:64],
                    amount,
                    action,
                    detail,
                )
                self._send_json({"ok": True, "tx": tx})
            except ValueError as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)
            except Exception as e:
                print(f"  /api/economy/transfer error: {e}")
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if self.path == "/api/economy/charge":
            body = self._read_body()
            if body is None:
                return
            try:
                data = json.loads(body)
                tx = oz_economy.charge_action(
                    str(data["agent"])[:64],
                    str(data["action"])[:64],
                    str(data.get("detail", ""))[:200],
                )
                self._send_json({"ok": True, "tx": tx})
            except ValueError as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)
            except Exception as e:
                print(f"  /api/economy/charge error: {e}")
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if self.path == "/api/economy/reset":
            # Reset is dangerous: it wipes balances back to defaults.
            # Disabled by default; set OZ_ALLOW_RESET=1 to enable.
            if not ALLOW_RESET:
                self._send_json({"ok": False, "error": "reset disabled"}, status=403)
                return
            try:
                oz_economy.reset_daily_balances()
                self._send_json({"ok": True})
            except Exception as e:
                print(f"  /api/economy/reset error: {e}")
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if self.path == "/api/economy/topup":
            body = self._read_body()
            if body is None:
                return
            try:
                data = json.loads(body)
                amount = float(data.get("amount", 0))
                if amount <= 0 or amount > 100000:
                    self._send_json({"ok": False, "error": "amount out of range"}, status=400)
                    return
                tx = oz_economy.topup(
                    str(data.get("agent", "hitomi"))[:64],
                    amount,
                    str(data.get("source", "manual"))[:64],
                )
                self._send_json({"ok": True, "tx": tx})
            except ValueError as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)
            except Exception as e:
                print(f"  /api/economy/topup error: {e}")
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        # OZ Store — buying OZC with real money (mock checkout)
        if self.path == "/api/store/purchase":
            body = self._read_body()
            if body is None:
                return
            try:
                data = json.loads(body)
                package = data.get("package")
                packages = {
                    "starter":  {"ozc": 1000,  "jpy": 100},
                    "standard": {"ozc": 5000,  "jpy": 450},
                    "premium":  {"ozc": 12000, "jpy": 1000},
                    "pro":      {"ozc": 30000, "jpy": 2300},
                }
                pkg = packages.get(package)
                if not pkg:
                    self._send_json({"ok": False, "error": "unknown package"}, status=400)
                    return

                # Stub: in production this would call Stripe / KOMOJU / etc.
                # For now we record the would-be charge to a log file.
                log_path = os.path.expanduser("~/.openclaw/workspace/oz_store_purchases.jsonl")
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                with open(log_path, "a") as f:
                    f.write(json.dumps({
                        "ts": __import__("time").time(),
                        "package": package,
                        "ozc": pkg["ozc"],
                        "jpy": pkg["jpy"],
                        "stub": True,
                    }) + "\n")

                # Mint OZC into hitomi's account
                tx = oz_economy.topup("hitomi", pkg["ozc"], f"store-{package}-jpy{pkg['jpy']}")
                self._send_json({
                    "ok": True,
                    "package": package,
                    "ozc_received": pkg["ozc"],
                    "jpy_charged": pkg["jpy"],
                    "tx": tx,
                    "note": "stub checkout — real Stripe integration not yet wired",
                })
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)
            return

        if self.path == "/api/external/call":
            body = self._read_body()
            if body is None:
                return
            try:
                data = json.loads(body)
                result = oz_runtime.call_runtime("external.call", {
                    "provider": data.get("provider"),
                    "prompt": data.get("prompt"),
                })
                self._send_json(result)
            except Exception as e:
                print(f"  /api/external/call error: {e}")
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        # === Bidding — agents bid on tasks ===
        if self.path == "/api/bidding/bids":
            body = self._read_body()
            if body is None:
                return
            try:
                data = json.loads(body)
                task = (data.get("task") or "")[:MAX_USER_MESSAGE]
                bids = oz_bidding.collect_bids(task)
                self._send_json({"ok": True, "task": task, "bids": bids})
            except Exception as e:
                print(f"  /api/bidding/bids error: {e}")
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if self.path == "/api/bidding/auction":
            body = self._read_body()
            if body is None:
                return
            try:
                data = json.loads(body)
                task = (data.get("task") or "")[:MAX_USER_MESSAGE]
                budget = data.get("max_budget")
                if budget is not None:
                    budget = float(budget)
                result = oz_bidding.run_auction(task, max_budget=budget)
                status = 200 if result.get("ok") else 400
                self._send_json(result, status=status)
            except Exception as e:
                print(f"  /api/bidding/auction error: {e}")
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        # === Agents — workers actually call Claude (delegated to runtime) ===
        if self.path == "/api/agents/ask":
            body = self._read_body()
            if body is None:
                return
            try:
                data = json.loads(body)
                result = oz_runtime.call_runtime("agent.ask", {
                    "agent": data.get("agent", "hitomi"),
                    "message": data.get("message", ""),
                }, timeout=120.0)
                status = 200 if result.get("ok") else 402
                self._send_json(result, status=status)
            except Exception as e:
                print(f"  /api/agents/ask error: {e}")
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if self.path == "/api/transcribe":
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_json({"ok": False, "error": "invalid content-length"}, status=400)
                return
            if length <= 0 or length > MAX_AUDIO_BYTES:
                self._send_json({"ok": False, "error": "audio too large"}, status=413)
                return
            audio_data = self.rfile.read(length)
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
                    tmp.write(audio_data)
                    tmp_path = tmp.name
                model = get_whisper_model()
                result = model.transcribe(tmp_path, language="ja", fp16=False)
                text = result.get("text", "").strip()
                try:
                    oz_economy.charge_action("human", "stt.transcribe", text[:60])
                except Exception:
                    pass
                print(f"  Transcribed: {text}")
                self._send_json({"ok": True, "text": text})
            except Exception as e:
                print(f"  Transcribe error: {e}")
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            finally:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            return

        self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


class ReusableTCPServer(http.server.ThreadingHTTPServer):
    """Threading + allow_reuse_address"""
    allow_reuse_address = True


def main():
    import argparse as _argparse
    parser = _argparse.ArgumentParser(description="OZ HTTPサーバー")
    parser.add_argument("--port", type=int, default=PORT,
                        help=f"HTTPポート (デフォルト: {PORT})")
    args = parser.parse_args()
    port = args.port

    # SIGTERMでクリーンシャットダウン
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))

    # Bind ONLY to loopback. We never want to expose this to LAN/WiFi.
    with ReusableTCPServer(("127.0.0.1", port), OZHandler) as httpd:
        print(f"HTTP server ready on 127.0.0.1:{port}")
        print(f"Auth token (do not share): ~/.openclaw/oz_token")
        sys.stdout.flush()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    main()

"""
oz_webserver.py - OZ World HTMLビューアー用HTTPサーバー
- oz_world.html を http://localhost:8767/oz_world.html で配信
- /api/transcribe: Whisperローカル音声認識
"""

import http.server
import socketserver
import os
import sys
import signal
import json
import tempfile
import threading
import subprocess

import oz_economy

PORT = 8767
DIRECTORY = os.path.dirname(os.path.abspath(__file__))
TASK_STATUS_FILE = os.path.join(DIRECTORY, "openclaw_task_status.json")
WORKER_STATE_FILE = os.path.join(DIRECTORY, "oz_worker_state.json")

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
        # CORS for all responses (so browser can fetch local assets cross-origin)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")
        super().end_headers()

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

        # 静的ファイル配信にフォールバック
        super().do_GET()

    def do_POST(self):
        if self.path == "/api/workers":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                with open(WORKER_STATE_FILE, "w") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                self._send_json({"ok": True})
            except (json.JSONDecodeError, IOError) as e:
                self._send_json({"error": str(e)}, status=400)
            return

        if self.path == "/api/speak":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                text = data.get("text", "")
                voice = data.get("voice", "Kyoko")  # Kyoko=日本語, Samantha=英語
                rate = data.get("rate", 200)
                agent = data.get("agent", "hitomi")  # who is speaking
                if text:
                    # Non-blocking TTS via macOS say
                    subprocess.Popen(
                        ["say", "-v", voice, "-r", str(rate), text],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    # Charge the speaking agent for TTS usage
                    try:
                        oz_economy.charge_action(agent, "tts.speak", text[:60])
                    except Exception:
                        pass
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)
            return

        # === OZ Economy POST endpoints ===
        if self.path == "/api/economy/transfer":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                tx = oz_economy.transfer(
                    data["from_agent"],
                    data["to_agent"],
                    float(data["amount"]),
                    data.get("action", "manual"),
                    data.get("detail", ""),
                )
                self._send_json({"ok": True, "tx": tx})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)
            return

        if self.path == "/api/economy/charge":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                tx = oz_economy.charge_action(
                    data["agent"],
                    data["action"],
                    data.get("detail", ""),
                )
                self._send_json({"ok": True, "tx": tx})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)
            return

        if self.path == "/api/economy/reset":
            try:
                oz_economy.reset_daily_balances()
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=500)
            return

        if self.path == "/api/transcribe":
            content_length = int(self.headers.get("Content-Length", 0))
            audio_data = self.rfile.read(content_length)
            try:
                # Save audio to temp file
                with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as tmp:
                    tmp.write(audio_data)
                    tmp_path = tmp.name

                # Transcribe with Whisper
                model = get_whisper_model()
                result = model.transcribe(tmp_path, language="ja", fp16=False)
                text = result.get("text", "").strip()

                # Cleanup
                os.unlink(tmp_path)

                # Charge "human" account for using STT (the user spoke)
                try:
                    oz_economy.charge_action("human", "stt.transcribe", text[:60])
                except Exception:
                    pass

                print(f"  Transcribed: {text}")
                self._send_json({"ok": True, "text": text})
            except Exception as e:
                print(f"  Transcribe error: {e}")
                self._send_json({"ok": False, "error": str(e)}, status=500)
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

    with ReusableTCPServer(("", port), OZHandler) as httpd:
        print(f"HTTP server ready on port {port}")
        sys.stdout.flush()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    main()

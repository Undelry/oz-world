"""
OZ Server - hitomiライブモニター付きサーバー
ポート: 8766
"""

import http.server
import json
import os
import subprocess
from urllib.parse import urlparse

STATUS_FILE = os.path.expanduser("~/.openclaw/workspace/hitomi_browser_status.json")
SCREENSHOT_FILE = os.path.expanduser("~/.openclaw/workspace/hitomi_screenshot.png")
JOBS_CACHE_FILE = os.path.expanduser("~/Desktop/agent-os/jobs_cache.json")
APPLIED_JOBS_FILE = os.path.expanduser("~/Desktop/agent-os/applied_jobs.json")
OIMO_STATE_FILE = os.path.expanduser("~/.openclaw/workspace/scripts/oimo_state.json")
OZ_DIR = os.path.dirname(os.path.abspath(__file__))


class OZHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=OZ_DIR, **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)

        # hitomiスクリーンショット（ライブ）
        if parsed.path == "/hitomi-screenshot":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                if os.path.exists(SCREENSHOT_FILE):
                    with open(SCREENSHOT_FILE, "rb") as f:
                        self.wfile.write(f.read())
            except Exception:
                pass
            return

        # hitomiステータスJSON
        if parsed.path == "/hitomi-status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                if os.path.exists(STATUS_FILE):
                    with open(STATUS_FILE) as f:
                        data = f.read()
                else:
                    data = json.dumps({"active": False, "action": "待機中"})
            except Exception:
                data = json.dumps({"active": False})
            self.wfile.write(data.encode())
            return

        # Upworkステータス
        if parsed.path == "/upwork-status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                result = {}

                # applied_jobs.json
                if os.path.exists(APPLIED_JOBS_FILE):
                    with open(APPLIED_JOBS_FILE) as f:
                        applied = json.load(f)
                    result["total_applied"] = len(applied)
                    result["recent_applied"] = [
                        {"title": j.get("title", "?"), "applied_at": j.get("applied_at", "")}
                        for j in applied[-5:][::-1]
                    ]
                else:
                    result["total_applied"] = 0
                    result["recent_applied"] = []

                # jobs_cache.json
                if os.path.exists(JOBS_CACHE_FILE):
                    with open(JOBS_CACHE_FILE) as f:
                        jobs_raw = json.load(f)
                    # jobs_cache may be list or dict
                    if isinstance(jobs_raw, list):
                        result["jobs_fetched"] = len(jobs_raw)
                    elif isinstance(jobs_raw, dict):
                        result["jobs_fetched"] = jobs_raw.get("total_fetched", len(jobs_raw.get("jobs", [])))
                    else:
                        result["jobs_fetched"] = 0
                else:
                    result["jobs_fetched"] = 0

                # oimo_state.json
                if os.path.exists(OIMO_STATE_FILE):
                    with open(OIMO_STATE_FILE) as f:
                        oimo = json.load(f)
                    result["last_run"] = oimo.get("last_run", "")
                    result["last_processed"] = oimo.get("last_processed", 0)
                    result["last_stats"] = oimo.get("last_stats", {})
                    result["connects_remaining"] = oimo.get("connects_remaining", None)
                else:
                    result["last_run"] = ""
                    result["last_processed"] = 0
                    result["last_stats"] = {}

                self.wfile.write(json.dumps(result).encode())
            except Exception as e:
                self.wfile.write(json.dumps({"error": str(e)}).encode())
            return

        # 通常のファイル配信
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)

        # エージェントチャット送信
        if parsed.path == "/chat":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                data = json.loads(body.decode())
                agent_id = data.get("agentId", "hitomi")
                message = data.get("message", "")
                if message:
                    # Telegramに送信
                    telegram_msg = f"@{agent_id}: {message}"
                    subprocess.Popen(
                        ["openclaw", "message", "send", "telegram:8643951982", telegram_msg],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                    self.wfile.write(json.dumps({"ok": True, "sent": telegram_msg}).encode())
                else:
                    self.wfile.write(json.dumps({"ok": False, "error": "empty message"}).encode())
            except Exception as e:
                self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode())
            return

        self.send_response(405)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        # ポーリングエンドポイントはログ省略
        if args and any(ep in str(args[0]) for ep in ["/hitomi-status", "/hitomi-screenshot", "/upwork-status"]):
            return
        super().log_message(format, *args)


if __name__ == "__main__":
    PORT = 8766
    with http.server.ThreadingHTTPServer(("", PORT), OZHandler) as httpd:
        print(f"OZ Server起動: http://localhost:{PORT}")
        httpd.serve_forever()

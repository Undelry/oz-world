"""
oz_webserver.py - OZ World HTMLビューアー用HTTPサーバー
- oz_world.html を http://localhost:8767/oz_world.html で配信
- ワークスペースディレクトリの静的ファイルを配信
"""

import http.server
import socketserver
import os
import sys
import signal
import json

PORT = 8767
DIRECTORY = os.path.dirname(os.path.abspath(__file__))
TASK_STATUS_FILE = os.path.join(DIRECTORY, "openclaw_task_status.json")
WORKER_STATE_FILE = os.path.join(DIRECTORY, "oz_worker_state.json")


class OZHandler(http.server.SimpleHTTPRequestHandler):
    """REST API + 静的ファイル配信ハンドラ"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

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

        self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


class ReusableTCPServer(socketserver.TCPServer):
    """allow_reuse_address を server_bind() の前に有効化する TCPServer"""
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

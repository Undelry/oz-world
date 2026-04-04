"""
oz_screencast.py - OZライブスクリーンキャスト サーバー
- PlaywrightでChromiumを起動し、CDPセッション経由でPage.startScreencastを実行
- WebSocketサーバー（port 8766）でフレームをOZビューアーにリレー
- ブラウザ操作中はライブ映像、非操作時はアイドル状態を通知
"""

import asyncio
import json
import time
import base64
import logging
from datetime import datetime
from playwright.async_api import async_playwright
import websockets
from browser_agent import create_browser

# websocketsライブラリのハンドシェイクエラーを抑制
logging.getLogger("websockets").setLevel(logging.ERROR)

# --- 設定 ---
WS_PORT = 8766
SCREENCAST_FPS = 15  # max fps
SCREENCAST_QUALITY = 70  # JPEG quality (1-100)
SCREENCAST_MAX_WIDTH = 1280
SCREENCAST_MAX_HEIGHT = 800
IDLE_TIMEOUT = 3.0  # seconds without new frame = idle

# --- 状態管理 ---
state = {
    "active": False,
    "last_frame_time": 0,
    "last_frame_data": None,
    "frame_count": 0,
    "url": "",
    "connected_viewers": set(),
    "browser_running": False,
}


class OZScreencastServer:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.cdp_session = None
        self.viewers = set()
        self.running = False
        self.last_frame_time = 0
        self.last_frame_data = None
        self.frame_count = 0
        self._idle_check_task = None

    async def start_browser(self, url="about:blank", profile_dir=None):
        """Chromiumを起動し、CDPスクリーンキャストを開始"""
        self.playwright, self.browser, self.context, self.page = await create_browser(
            headless=False,
            profile_dir=profile_dir,
            viewport=(SCREENCAST_MAX_WIDTH, SCREENCAST_MAX_HEIGHT),
        )

        # CDPセッションを取得してスクリーンキャスト開始
        self.cdp_session = await self.page.context.new_cdp_session(self.page)

        # スクリーンキャストフレームのイベントハンドラ
        self.cdp_session.on("Page.screencastFrame", self._on_screencast_frame)

        # スクリーンキャスト開始
        await self.cdp_session.send("Page.startScreencast", {
            "format": "jpeg",
            "quality": SCREENCAST_QUALITY,
            "maxWidth": SCREENCAST_MAX_WIDTH,
            "maxHeight": SCREENCAST_MAX_HEIGHT,
            "everyNthFrame": max(1, 60 // SCREENCAST_FPS),
        })

        state["browser_running"] = True
        self.running = True
        print(f"🖥️  ブラウザ起動 — CDPスクリーンキャスト開始 ({SCREENCAST_FPS}fps, quality={SCREENCAST_QUALITY})")

        if url != "about:blank":
            await self.goto(url)

        # アイドル検出タスク開始
        self._idle_check_task = asyncio.create_task(self._idle_checker())

    async def goto(self, url, wait_until="domcontentloaded", timeout=60000):
        """ページ遷移"""
        await self.page.goto(url, wait_until=wait_until, timeout=timeout)
        state["url"] = url
        print(f"🔗 移動: {url}")

    async def _on_screencast_frame(self, params):
        """CDPからスクリーンキャストフレームを受信"""
        session_id = params.get("sessionId")
        frame_data = params.get("data")  # base64 JPEG
        metadata = params.get("metadata", {})

        # フレームACKを送信（次のフレームを受け取るために必須）
        try:
            await self.cdp_session.send("Page.screencastFrameAck", {
                "sessionId": session_id,
            })
        except Exception:
            pass

        if not frame_data:
            return

        now = time.time()
        self.last_frame_time = now
        self.last_frame_data = frame_data
        self.frame_count += 1

        # アクティブ状態に更新
        was_idle = not state["active"]
        state["active"] = True
        state["last_frame_time"] = now
        state["frame_count"] = self.frame_count

        # 接続中の全ビューアーにフレーム送信
        if self.viewers:
            message = json.dumps({
                "type": "frame",
                "data": frame_data,
                "timestamp": now,
                "metadata": {
                    "width": metadata.get("pageScaleFactor", 1) * metadata.get("deviceWidth", SCREENCAST_MAX_WIDTH),
                    "height": metadata.get("pageScaleFactor", 1) * metadata.get("deviceHeight", SCREENCAST_MAX_HEIGHT),
                    "url": state["url"],
                    "frameNumber": self.frame_count,
                },
                "active": True,
            })
            # 全ビューアーに並列送信
            await asyncio.gather(
                *[self._safe_send(ws, message) for ws in self.viewers],
                return_exceptions=True,
            )

        if was_idle:
            print(f"▶️  アクティブ状態に復帰 (frame #{self.frame_count})")

    async def _idle_checker(self):
        """定期的にアイドル状態を検出"""
        while self.running:
            await asyncio.sleep(1.0)
            now = time.time()
            if state["active"] and (now - self.last_frame_time) > IDLE_TIMEOUT:
                state["active"] = False
                print(f"⏸️  アイドル状態に移行 ({IDLE_TIMEOUT}秒間フレームなし)")
                # アイドル通知を送信
                if self.viewers:
                    message = json.dumps({
                        "type": "idle",
                        "timestamp": now,
                    })
                    await asyncio.gather(
                        *[self._safe_send(ws, message) for ws in self.viewers],
                        return_exceptions=True,
                    )

    async def _safe_send(self, ws, message):
        """WebSocket送信（切断済みのビューアーは除去）"""
        try:
            await ws.send(message)
        except websockets.exceptions.ConnectionClosed:
            self.viewers.discard(ws)

    async def handle_viewer(self, websocket):
        """OZビューアーからのWebSocket接続を処理"""
        self.viewers.add(websocket)
        remote = websocket.remote_address
        print(f"👁️  ビューアー接続: {remote} (合計: {len(self.viewers)})")

        # 接続時に現在の状態を送信
        welcome = {
            "type": "welcome",
            "active": state["active"],
            "browserRunning": state["browser_running"],
            "url": state["url"],
            "timestamp": time.time(),
        }
        # 最新フレームがあれば一緒に送信
        if self.last_frame_data and state["active"]:
            welcome["lastFrame"] = self.last_frame_data
        await websocket.send(json.dumps(welcome))

        try:
            async for message in websocket:
                # ビューアーからのコマンド（将来拡張用）
                try:
                    cmd = json.loads(message)
                    if cmd.get("type") == "ping":
                        await websocket.send(json.dumps({"type": "pong"}))
                    elif cmd.get("type") == "goto":
                        url = cmd.get("url", "")
                        if url:
                            await self.goto(url)
                except json.JSONDecodeError:
                    pass
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.viewers.discard(websocket)
            print(f"👁️  ビューアー切断: {remote} (残り: {len(self.viewers)})")

    async def stop(self):
        """ブラウザとサーバーを停止"""
        self.running = False
        if self._idle_check_task:
            self._idle_check_task.cancel()
        if self.cdp_session:
            try:
                await self.cdp_session.send("Page.stopScreencast")
            except Exception:
                pass
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        state["browser_running"] = False
        print("🛑 ブラウザ停止")


async def main():
    import sys
    import argparse as _argparse

    parser = _argparse.ArgumentParser(description="OZライブスクリーンキャスト")
    parser.add_argument("url", nargs="?",
                        default="https://www.upwork.com/nx/find-work/best-matches")
    parser.add_argument("--port", type=int, default=WS_PORT,
                        help=f"WebSocketポート (デフォルト: {WS_PORT})")
    parser.add_argument("--profile-dir", default=None,
                        help="ブラウザプロファイルディレクトリ")
    args = parser.parse_args()

    ws_port = args.port
    url = args.url

    server = OZScreencastServer()

    # WebSocketサーバー起動（handshakeエラーを抑制）
    ws_server = await websockets.serve(
        server.handle_viewer,
        "localhost",
        ws_port,
        max_size=10 * 1024 * 1024,  # 10MB max message
        logger=logging.getLogger("oz_ws"),
    )
    # 非WebSocket接続のエラーログを抑制
    logging.getLogger("oz_ws").setLevel(logging.CRITICAL)
    print(f"🌐 OZ WebSocketサーバー起動: ws://localhost:{ws_port}")

    # ブラウザ起動＆スクリーンキャスト開始
    await server.start_browser(url, profile_dir=args.profile_dir)

    print(f"\n✅ OZライブスクリーンキャスト稼働中")
    print(f"   ビューアー接続先: ws://localhost:{ws_port}")
    print(f"   OZワールド: oz_world.html をブラウザで開いてください")
    print(f"   終了: Ctrl+C\n")
    sys.stdout.flush()

    try:
        # 永続稼働
        await asyncio.Future()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n終了処理中...")
    finally:
        await server.stop()
        ws_server.close()
        await ws_server.wait_closed()
        print("完了")


if __name__ == "__main__":
    asyncio.run(main())

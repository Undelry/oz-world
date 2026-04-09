"""
oz_launcher.py - OZライブビューア一括起動スクリプト
1. 既存の oz_screencast.py / oz_webserver.py プロセスを確実に停止
2. oz_webserver.py (HTTP port 8767) を起動し、リクエスト受付可能になるまで待機
3. oz_screencast.py (WS port 8766) を起動し、リクエスト受付可能になるまで待機
4. Arc ブラウザで http://localhost:8767/oz_world.html を開く
5. 起動後の自動確認 — スクリーンショット撮影＆ステータスJSON書き出し
"""

import subprocess
import sys
import time
import signal
import os
import socket
import urllib.request
import json
from datetime import datetime

WORKSPACE = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = os.path.expanduser("~/Desktop/agent-os/venv/bin/python3")
# venvのPythonが存在すればそちらを使用（websockets/playwright対応）
PYTHON = VENV_PYTHON if os.path.isfile(VENV_PYTHON) else sys.executable

HTTP_PORT = 8767  # default, overridable via --http-port
WS_PORT = 8766    # default, overridable via --ws-port
OZ_URL = f"http://localhost:{HTTP_PORT}/oz_world.html"
STATUS_FILE = os.path.join(WORKSPACE, "hitomi_browser_status.json")
TASK_STATUS_FILE = os.path.join(WORKSPACE, "openclaw_task_status.json")

# 起動したサブプロセスを保持（クリーンアップ用）
children: list[subprocess.Popen] = []


def write_status(status: str, details: dict = None):
    """hitomiが読み取るステータスJSONを更新"""
    data = {
        "timestamp": datetime.now().isoformat(),
        "status": status,
        "oz_url": OZ_URL,
        "http_port": HTTP_PORT,
        "ws_port": WS_PORT,
        "processes": {
            "webserver": any(p.poll() is None for p in children[:1]) if children else False,
            "screencast": any(p.poll() is None for p in children[1:2]) if len(children) > 1 else False,
        },
    }
    if details:
        data.update(details)
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"  WARNING: ステータスファイル書き出し失敗: {e}")


def kill_existing_processes():
    """既存の oz_screencast.py / oz_webserver.py プロセスを停止"""
    targets = ["oz_screencast.py", "oz_webserver.py"]
    for name in targets:
        # pgrep で対象PIDを取得
        try:
            result = subprocess.run(
                ["pgrep", "-f", name],
                capture_output=True, text=True, timeout=5,
            )
            pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
        except Exception:
            pids = []

        # 自分自身とランチャープロセスのPIDを除外
        my_pid = str(os.getpid())
        parent_pid = str(os.getppid())
        pids = [p for p in pids if p not in (my_pid, parent_pid)]

        if not pids:
            continue

        print(f"  既存プロセスを停止: {name} (PID: {', '.join(pids)})")

        # まず SIGTERM で穏やかに停止
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

        # 最大3秒待ってまだ生きていたら SIGKILL
        deadline = time.time() + 3
        while time.time() < deadline:
            alive = []
            for pid in pids:
                try:
                    os.kill(int(pid), 0)  # 存在チェック
                    alive.append(pid)
                except (ProcessLookupError, PermissionError):
                    pass
            if not alive:
                break
            time.sleep(0.2)
        else:
            for pid in alive:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                    print(f"  SIGKILL送信: PID {pid}")
                except (ProcessLookupError, PermissionError):
                    pass

    # ポートが解放されるまで少し待つ
    for port in [HTTP_PORT, WS_PORT]:
        wait_port_free(port, timeout=3)


def wait_port_free(port, timeout=3):
    """ポートが解放されるまで待機"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("localhost", port)) != 0:
                return True
        time.sleep(0.2)
    return False


def wait_port_open(port, timeout=15):
    """ポートが接続可能になるまで待機"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            if s.connect_ex(("localhost", port)) == 0:
                return True
        time.sleep(0.3)
    return False


def wait_http_ready(url, timeout=10):
    """HTTP GETが200を返すまで待機"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.urlopen(url, timeout=2)
            if req.status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def start_webserver():
    """oz_webserver.py を起動"""
    print("  oz_webserver.py を起動中...")
    proc = subprocess.Popen(
        [PYTHON, "-u", os.path.join(WORKSPACE, "oz_webserver.py"),
         "--port", str(HTTP_PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=WORKSPACE,
    )
    children.append(proc)

    # stdout から "ready" メッセージを待つ（最大5秒）
    import selectors
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.time() + 5
    while time.time() < deadline:
        events = sel.select(timeout=0.5)
        for key, _ in events:
            line = key.fileobj.readline()
            if line:
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    print(f"    {text}")
                if "ready" in text.lower():
                    sel.close()
                    return proc
        if proc.poll() is not None:
            print(f"  ERROR: oz_webserver.py が異常終了 (code={proc.returncode})")
            sel.close()
            return None
    sel.close()

    # readyメッセージが来なくてもポートが開いていればOK
    if wait_port_open(HTTP_PORT, timeout=3):
        return proc

    print(f"  WARNING: HTTP port {HTTP_PORT} がタイムアウト")
    return proc


def start_screencast(url="https://www.upwork.com/nx/find-work/best-matches"):
    """oz_screencast.py を起動"""
    print("  oz_screencast.py を起動中...")
    proc = subprocess.Popen(
        [PYTHON, "-u", os.path.join(WORKSPACE, "oz_screencast.py"),
         "--port", str(WS_PORT), url],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=WORKSPACE,
    )
    children.append(proc)

    # stdoutから起動完了メッセージを待つ（最大30秒 — Playwright起動に時間がかかる）
    import selectors
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.time() + 30
    while time.time() < deadline:
        events = sel.select(timeout=0.5)
        for key, _ in events:
            line = key.fileobj.readline()
            if line:
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    print(f"    {text}")
                if "稼働中" in text or "WebSocket" in text.lower():
                    # もう少し待ってWSポートが完全に開くのを確認
                    if wait_port_open(WS_PORT, timeout=5):
                        sel.close()
                        return proc
        if proc.poll() is not None:
            print(f"  ERROR: oz_screencast.py が異常終了 (code={proc.returncode})")
            sel.close()
            return None
    sel.close()

    if wait_port_open(WS_PORT, timeout=3):
        return proc

    print(f"  WARNING: WS port {WS_PORT} がタイムアウト")
    return proc


def open_in_arc(url):
    """Arcブラウザで確実にURLを開く"""
    print(f"  Arc で {url} を開きます...")

    # 方法1: osascript で Arc にURLを開かせる（最も確実）
    script = f'''
    tell application "Arc"
        activate
        delay 0.5
        tell front window
            make new tab with properties {{URL:"{url}"}}
        end tell
    end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            print("  Arc で新しいタブを開きました")
            return True
        else:
            print(f"  osascript fallback: {result.stderr.strip()}")
    except Exception as e:
        print(f"  osascript error: {e}")

    # 方法2: open コマンド（フォールバック）
    try:
        subprocess.run(["open", "-a", "Arc", url], check=True, timeout=10)
        print("  open コマンドで Arc を起動しました")
        return True
    except Exception as e:
        print(f"  open コマンド失敗: {e}")

    # 方法3: デフォルトブラウザで開く（最終フォールバック）
    try:
        subprocess.run(["open", url], check=True, timeout=10)
        print("  デフォルトブラウザで開きました")
        return True
    except Exception:
        print("  ERROR: ブラウザを開けませんでした")
        return False


def verify_oz_launch():
    """OZが正常に起動したことを自動確認し、ステータスを報告"""
    print("\n[5/5] OZ起動確認中...")
    checks = {
        "http_server": False,
        "ws_server": False,
        "html_content": False,
        "arc_browser": False,
        "screenshot": None,
    }

    # 1. HTTPサーバーの応答確認
    if wait_http_ready(OZ_URL, timeout=5):
        checks["http_server"] = True
        print("  [OK] HTTPサーバー応答確認")
    else:
        print("  [NG] HTTPサーバー応答なし")

    # 2. HTMLコンテンツの検証（3Dシーン要素が含まれているか）
    try:
        req = urllib.request.urlopen(OZ_URL, timeout=5)
        html = req.read().decode("utf-8", errors="replace")
        expected_markers = ["three-container", "OZ — Virtual World", "THREE.Scene", "WebSocket"]
        found = [m for m in expected_markers if m in html]
        if len(found) >= 3:
            checks["html_content"] = True
            print(f"  [OK] HTMLコンテンツ検証OK ({len(found)}/{len(expected_markers)} markers)")
        else:
            print(f"  [NG] HTMLコンテンツ不完全 ({len(found)}/{len(expected_markers)} markers)")
    except Exception as e:
        print(f"  [NG] HTMLコンテンツ取得失敗: {e}")

    # 3. WebSocketサーバーの接続確認
    if wait_port_open(WS_PORT, timeout=5):
        checks["ws_server"] = True
        print("  [OK] WebSocketサーバー接続確認")
    else:
        print("  [NG] WebSocketサーバー接続不可")

    # 4. Arcブラウザでページが表示されているか確認（screencapture）
    screenshot_path = os.path.join(WORKSPACE, "hitomi_screenshot.png")
    try:
        # Non-invasive screenshot — don't activate Arc (would steal user focus)
        result = subprocess.run(
            ["screencapture", "-x", screenshot_path],
            capture_output=True, timeout=10,
        )

        if os.path.isfile(screenshot_path) and os.path.getsize(screenshot_path) > 1000:
            checks["screenshot"] = screenshot_path
            checks["arc_browser"] = True
            print(f"  [OK] スクリーンショット保存: {screenshot_path}")
        else:
            print("  [NG] スクリーンショット撮影失敗")
    except Exception as e:
        print(f"  [NG] スクリーンショット撮影エラー: {e}")

    # 5. 子プロセスの生存確認
    alive_procs = sum(1 for p in children if p.poll() is None)
    checks["alive_processes"] = alive_procs
    print(f"  [INFO] 稼働中プロセス: {alive_procs}/{len(children)}")

    # 総合判定
    core_ok = checks["http_server"] and checks["ws_server"] and checks["html_content"]
    all_ok = core_ok and checks["arc_browser"]
    checks["overall"] = "OK" if all_ok else "PARTIAL" if core_ok else "FAILED"

    # 判定理由を明示
    issues = []
    if not checks["http_server"]:  issues.append("HTTPサーバー応答なし")
    if not checks["ws_server"]:    issues.append("WebSocketサーバー接続不可")
    if not checks["html_content"]: issues.append("HTMLコンテンツ不完全")
    if not checks["arc_browser"]:  issues.append("ブラウザ確認不可")

    if all_ok:
        msg = "OZ起動完了 — 全サービス正常稼働中"
    elif core_ok:
        msg = f"OZ起動（一部未確認）: {', '.join(issues)}"
    else:
        msg = f"OZ起動失敗: {', '.join(issues)}"

    print(f"\n  総合判定: {checks['overall']}")
    print(f"  詳細: {msg}")

    # ステータスJSON書き出し
    write_status(
        status=checks["overall"],
        details={
            "checks": checks,
            "screenshot_path": checks.get("screenshot"),
            "message": msg,
            "issues": issues,
        },
    )
    return checks


def cleanup(signum=None, frame=None):
    """子プロセスをすべて停止"""
    print("\n終了処理中...")
    write_status("SHUTTING_DOWN")
    for proc in children:
        if proc.poll() is None:
            proc.terminate()
    # 2秒待って生存しているプロセスをkill
    deadline = time.time() + 2
    while time.time() < deadline:
        if all(p.poll() is not None for p in children):
            break
        time.sleep(0.2)
    for proc in children:
        if proc.poll() is None:
            proc.kill()
    write_status("STOPPED")
    print("完了")
    sys.exit(0)


def _is_task_manager_running() -> bool:
    """Check if task_manager.py is already managing tasks."""
    if not os.path.isfile(TASK_STATUS_FILE):
        return False
    try:
        with open(TASK_STATUS_FILE) as f:
            data = json.load(f)
        mgr_pid = data.get("manager_pid")
        if mgr_pid:
            os.kill(mgr_pid, 0)  # Check if process is alive
            return True
    except (json.JSONDecodeError, ProcessLookupError, PermissionError, TypeError):
        pass
    return False


def main():
    global HTTP_PORT, WS_PORT, OZ_URL

    import argparse
    parser = argparse.ArgumentParser(description="OZライブビューア一括起動")
    parser.add_argument("--url", default="https://www.upwork.com/nx/find-work/best-matches",
                        help="スクリーンキャスト対象URL")
    parser.add_argument("--no-arc", action="store_true",
                        help="Arcブラウザを自動で開かない")
    parser.add_argument("--http-port", type=int, default=HTTP_PORT,
                        help=f"HTTPサーバーポート (デフォルト: {HTTP_PORT})")
    parser.add_argument("--ws-port", type=int, default=WS_PORT,
                        help=f"WebSocketポート (デフォルト: {WS_PORT})")
    args = parser.parse_args()

    # ポートをCLI引数で上書き
    HTTP_PORT = args.http_port
    WS_PORT = args.ws_port
    OZ_URL = f"http://localhost:{HTTP_PORT}/oz_world.html"

    # タスクマネージャーが稼働中なら委譲
    if _is_task_manager_running():
        print("TaskManager が稼働中 — task_manager.py 経由で管理してください")
        print(f"  python task_manager.py start oz_screencast")
        print(f"  python task_manager.py status")
        return

    # Ctrl+C / SIGTERM でクリーンアップ
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    write_status("STARTING")

    print("=" * 50)
    print("OZ ライブビューア — 起動シーケンス")
    print("=" * 50)

    # Step 1: 既存プロセスを停止
    print("\n[1/5] 既存プロセスを停止...")
    kill_existing_processes()
    print("  完了")

    # Step 2: HTTPサーバー起動
    print(f"\n[2/5] HTTPサーバー起動 (port {HTTP_PORT})...")
    web_proc = start_webserver()
    if web_proc is None or web_proc.poll() is not None:
        print("  FATAL: HTTPサーバーの起動に失敗しました")
        write_status("FAILED", {"error": "HTTP server failed to start"})
        cleanup()
        return

    # HTTPで実際にページが取得できることを確認
    if wait_http_ready(OZ_URL, timeout=5):
        print(f"  HTTP確認OK: {OZ_URL}")
    else:
        print(f"  WARNING: {OZ_URL} の応答確認がタイムアウト")

    # Step 3: スクリーンキャストサーバー起動
    print(f"\n[3/5] スクリーンキャストサーバー起動 (WS port {WS_PORT})...")
    sc_proc = start_screencast(args.url)
    if sc_proc is None or sc_proc.poll() is not None:
        print("  FATAL: スクリーンキャストサーバーの起動に失敗しました")
        write_status("FAILED", {"error": "Screencast server failed to start"})
        cleanup()
        return

    # Step 4: Arc でビューアーを開く
    if not args.no_arc:
        print(f"\n[4/5] Arc ブラウザでビューアーを開く...")
        # サーバーが安定するまで少し待つ
        time.sleep(1)
        open_in_arc(OZ_URL)
    else:
        print(f"\n[4/5] スキップ (--no-arc)")
        print(f"  手動でアクセス: {OZ_URL}")

    # Step 5: 起動確認（スクリーンショット＆ステータス書き出し）
    time.sleep(2)  # ブラウザのレンダリング完了を待つ
    checks = verify_oz_launch()

    print("\n" + "=" * 50)
    print(f"OZ ライブビューア稼働中 [{checks['overall']}]")
    print(f"  ビューアー: {OZ_URL}")
    print(f"  WebSocket:  ws://localhost:{WS_PORT}")
    print(f"  ステータス: {STATUS_FILE}")
    print("  終了: Ctrl+C")
    print("=" * 50)

    write_status("RUNNING")

    # 子プロセスの出力を中継しながら永続待機 + 定期ヘルスチェック
    last_health_check = time.time()
    try:
        while True:
            for proc in children:
                if proc.poll() is not None:
                    print(f"\n  WARNING: プロセス PID {proc.pid} が終了 (code={proc.returncode})")
                    children.remove(proc)
                    write_status("DEGRADED", {"lost_process_pid": proc.pid})
                    if not children:
                        print("  全プロセスが終了しました")
                        write_status("ALL_PROCESSES_EXITED")
                        return
                    break

            # 60秒ごとにヘルスチェック
            now = time.time()
            if now - last_health_check > 60:
                last_health_check = now
                http_ok = wait_http_ready(OZ_URL, timeout=3)
                ws_ok = wait_port_open(WS_PORT, timeout=3)
                alive = sum(1 for p in children if p.poll() is None)
                write_status(
                    "RUNNING" if (http_ok and ws_ok) else "DEGRADED",
                    {
                        "health": {
                            "http": http_ok,
                            "ws": ws_ok,
                            "alive_processes": alive,
                            "last_check": datetime.now().isoformat(),
                        }
                    },
                )

            time.sleep(1)
    except KeyboardInterrupt:
        cleanup()


if __name__ == "__main__":
    main()

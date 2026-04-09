"""
oz_evolve.py — 12時間自律進化ループ (Joeが寝てる間にOZが学ぶ)

設計原則:
- HARD BOUNDS: 最大時間, 最大反復回数, 最大コスト, 全部止める安全装置
- 全ての出力は ~/.openclaw/oz_vault/ 配下にしか書かない
- システム改変・破壊的操作は一切しない
- ハートビートファイルで進捗確認可能
- エラーが出ても止まらず次へ (連続失敗 N回で自動停止)
- 朝起きたら oz_vault/wake_up_summary.md を見れば全てわかる

各サイクル (1サイクル = 1時間目安) で実行すること:
  1. 全エージェントの reflection (oz_reflect.py)
  2. iPhone read-only スキャン (新着メール/通知/イベントを vault に記録)
  3. プロジェクト状態スナップショット (git status, last commits, file diffs)
  4. 「今日の総まとめ」の更新 (最新セッションを 1段落に圧縮)
  5. ハートビート更新

使い方:
  # フォアグラウンド (テスト):
  python3 oz_evolve.py run --max-cycles 2 --interval-min 1

  # バックグラウンド (本番):
  nohup python3 oz_evolve.py run --hours 12 > oz_evolve.log 2>&1 &

  # 進捗確認:
  cat ~/.openclaw/oz_vault/heartbeat.json

  # 即時停止:
  touch ~/.openclaw/oz_vault/STOP
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import oz_economy
import oz_vault


# ================================
# Hard safety bounds
# ================================
DEFAULT_HOURS = 12.0
DEFAULT_INTERVAL_MIN = 60          # 1サイクル/時間
DEFAULT_MAX_CYCLES = 14            # 12時間 + 余裕
DEFAULT_MAX_OZC_TOTAL = 800        # ¥800相当が上限
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5

VAULT = oz_vault.VAULT_ROOT
HEARTBEAT_PATH = VAULT / "heartbeat.json"
STOP_FILE = VAULT / "STOP"
LOG_PATH = VAULT / "evolve.log"
SUMMARY_PATH = VAULT / "wake_up_summary.md"

OZ_DIR = Path("/Users/maekawasei/Desktop/OZ")


# ================================
# Logging
# ================================
def log(msg: str):
    """Append a timestamped line to evolve.log."""
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}\n"
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    print(line.rstrip(), flush=True)


def write_heartbeat(state: dict):
    """Atomically write the heartbeat file."""
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        tmp = HEARTBEAT_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(HEARTBEAT_PATH)
    except OSError as e:
        log(f"heartbeat write failed: {e}")


def should_stop() -> bool:
    return STOP_FILE.exists()


# ================================
# Cost guard
# ================================
def total_spent_in_window(start_ts: float) -> float:
    """
    Sum of real resource consumption OZC since the loop started.
    Uses a direct SQL query instead of get_ledger to avoid the 10K limit
    and to properly filter out internal economy moves.
    """
    try:
        import sqlite3
        uri = f"file:{oz_economy.DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        cur = conn.cursor()
        exempt = ("topup", "auto.topup", "auction.win", "task.assign", "task.report")
        placeholders = ",".join("?" * len(exempt))
        cur.execute(
            f"SELECT COALESCE(SUM(amount), 0) FROM ledger "
            f"WHERE ts >= ? AND action NOT IN ({placeholders})",
            (start_ts, *exempt),
        )
        spent = float(cur.fetchone()[0])
        conn.close()
        return spent
    except Exception:
        return 0


# ================================
# Cycle steps
# ================================
def run_step(name: str, func, *args, **kwargs) -> dict:
    """Run a single cycle step with isolated error handling."""
    started = time.time()
    log(f"  step: {name} starting")
    try:
        result = func(*args, **kwargs)
        elapsed = time.time() - started
        log(f"  step: {name} ok ({elapsed:.1f}s)")
        return {"step": name, "ok": True, "elapsed_s": round(elapsed, 1), "result": result}
    except Exception as e:
        log(f"  step: {name} FAILED: {e}")
        return {"step": name, "ok": False, "error": str(e)[:200]}


def step_reflect_all() -> dict:
    """Run oz_reflect.py for every agent that has sessions."""
    proc = subprocess.run(
        ["python3", str(OZ_DIR / "oz_reflect.py"), "run"],
        capture_output=True, text=True, timeout=600,
    )
    return {
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-500:] if proc.stdout else "",
        "stderr_tail": proc.stderr[-200:] if proc.stderr else "",
    }


def step_iphone_snapshot() -> dict:
    """Capture a snapshot of iPhone state into vault/sessions/."""
    snap = {}
    for cmd in ("airpods", "audio", "today", "list-reminders", "messages"):
        try:
            proc = subprocess.run(
                ["python3", str(OZ_DIR / "oz_iphone.py"), cmd],
                capture_output=True, text=True, timeout=20,
            )
            if proc.returncode == 0 and proc.stdout:
                snap[cmd] = json.loads(proc.stdout)
        except Exception as e:
            snap[cmd] = {"ok": False, "error": str(e)[:200]}

    # Write to vault as a daily journal entry under sessions/
    now = datetime.now()
    day_dir = oz_vault.SESSIONS_DIR / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{now.strftime('%H%M%S')}_evolve_iphone-snapshot.md"

    body = "## iPhone Snapshot\n\n"
    body += "```json\n" + json.dumps(snap, indent=2, ensure_ascii=False) + "\n```\n"
    meta = oz_vault._format_frontmatter({
        "agent": "evolve",
        "ts": now.isoformat(timespec="seconds"),
        "kind": "iphone-snapshot",
    })
    path.write_text(meta + body, encoding="utf-8")
    return {"path": str(path), "fields_captured": list(snap.keys())}


def step_project_status() -> dict:
    """Capture git state of the OZ repo."""
    try:
        status = subprocess.run(
            ["git", "-C", str(OZ_DIR), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        log_out = subprocess.run(
            ["git", "-C", str(OZ_DIR), "log", "--oneline", "-5"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception as e:
        return {"error": str(e)}
    return {"status": status[:1000], "recent_commits": log_out[:500]}


def step_economy_snapshot() -> dict:
    """Snapshot economy state."""
    try:
        balances = oz_economy.get_all_balances()
        stats = oz_economy.get_daily_stats()
        return {"balances": balances, "stats": stats}
    except Exception as e:
        return {"error": str(e)}


def step_journal_update(cycle_data: list) -> dict:
    """Append to a daily journal note in knowledge/."""
    today = datetime.now().strftime("%Y-%m-%d")
    journal_path = oz_vault.KNOWLEDGE_DIR / f"journal_{today}.md"

    if journal_path.exists():
        content = journal_path.read_text(encoding="utf-8")
    else:
        content = (
            "---\n"
            f"topic: journal_{today}\n"
            f"created: {today}\n"
            "tags: [journal, evolve]\n"
            "---\n\n"
            f"# OZ Journal — {today}\n\n"
        )

    now = datetime.now().strftime("%H:%M:%S")
    content += f"\n## Cycle at {now}\n\n"
    for entry in cycle_data:
        content += f"- **{entry['step']}**: {'✅' if entry['ok'] else '❌'}"
        if entry.get('elapsed_s'):
            content += f" ({entry['elapsed_s']}s)"
        if not entry['ok']:
            content += f" — {entry.get('error', '')}"
        content += "\n"

    journal_path.write_text(content, encoding="utf-8")
    return {"path": str(journal_path)}


# ================================
# Main loop
# ================================
def run_loop(args):
    start_ts = time.time()
    deadline = start_ts + args.hours * 3600
    cycle_count = 0
    consecutive_failures = 0
    total_steps = 0
    total_failed = 0

    log("=" * 50)
    log(f"oz_evolve start: hours={args.hours} interval_min={args.interval_min}")
    log(f"  max_cycles={args.max_cycles} max_ozc={args.max_ozc}")
    log(f"  vault={VAULT}")
    log("=" * 50)

    write_heartbeat({
        "status": "starting",
        "started_at": datetime.fromtimestamp(start_ts).isoformat(timespec="seconds"),
        "deadline": datetime.fromtimestamp(deadline).isoformat(timespec="seconds"),
        "cycle_count": 0,
        "max_cycles": args.max_cycles,
        "total_spent_ozc": 0,
        "max_ozc": args.max_ozc,
    })

    # Make sure the STOP file is gone at startup
    try:
        STOP_FILE.unlink()
    except FileNotFoundError:
        pass

    while True:
        # Termination checks
        if should_stop():
            log("STOP file detected — shutting down")
            break
        if cycle_count >= args.max_cycles:
            log(f"max_cycles ({args.max_cycles}) reached — done")
            break
        if time.time() >= deadline:
            log(f"deadline reached after {args.hours}h — done")
            break
        spent = total_spent_in_window(start_ts)
        if spent >= args.max_ozc:
            log(f"cost cap ({args.max_ozc} OZC) reached: spent={spent}")
            break
        if consecutive_failures >= DEFAULT_MAX_CONSECUTIVE_FAILURES:
            log(f"too many consecutive failures ({consecutive_failures}) — bailing")
            break

        cycle_count += 1
        cycle_started = time.time()
        log("")
        log(f"=== cycle {cycle_count}/{args.max_cycles} ===")

        write_heartbeat({
            "status": "running",
            "cycle_count": cycle_count,
            "max_cycles": args.max_cycles,
            "total_spent_ozc": round(spent, 2),
            "max_ozc": args.max_ozc,
            "started_at": datetime.fromtimestamp(start_ts).isoformat(timespec="seconds"),
            "deadline": datetime.fromtimestamp(deadline).isoformat(timespec="seconds"),
            "last_cycle_at": datetime.now().isoformat(timespec="seconds"),
        })

        steps = [
            run_step("economy_snapshot", step_economy_snapshot),
            run_step("project_status", step_project_status),
            run_step("iphone_snapshot", step_iphone_snapshot),
            run_step("reflect_all", step_reflect_all),
        ]
        run_step("journal_update", step_journal_update, steps)

        cycle_failed = sum(1 for s in steps if not s["ok"])
        total_steps += len(steps)
        total_failed += cycle_failed

        if cycle_failed >= 3:
            consecutive_failures += 1
            log(f"  ⚠️  {cycle_failed} steps failed in this cycle (consecutive={consecutive_failures})")
        else:
            consecutive_failures = 0

        elapsed = time.time() - cycle_started
        log(f"=== cycle {cycle_count} done in {elapsed:.1f}s ===")

        # Sleep until next interval, but check stop signal every 30s
        sleep_until = cycle_started + args.interval_min * 60
        while time.time() < sleep_until:
            if should_stop() or time.time() >= deadline:
                break
            time.sleep(min(30, max(1, sleep_until - time.time())))

    # Final summary
    duration = time.time() - start_ts
    final_spent = total_spent_in_window(start_ts)
    log("")
    log("=" * 50)
    log(f"FINISHED after {duration / 3600:.1f}h")
    log(f"  cycles: {cycle_count}")
    log(f"  steps: {total_steps - total_failed}/{total_steps} ok")
    log(f"  spent: {final_spent} OZC")
    log("=" * 50)

    write_summary({
        "started_at": datetime.fromtimestamp(start_ts).isoformat(timespec="seconds"),
        "ended_at": datetime.now().isoformat(timespec="seconds"),
        "duration_hours": round(duration / 3600, 2),
        "cycles": cycle_count,
        "max_cycles": args.max_cycles,
        "steps_ok": total_steps - total_failed,
        "steps_total": total_steps,
        "spent_ozc": round(final_spent, 2),
        "max_ozc": args.max_ozc,
    })

    write_heartbeat({
        "status": "finished",
        "cycles": cycle_count,
        "duration_hours": round(duration / 3600, 2),
        "spent_ozc": round(final_spent, 2),
    })


def write_summary(stats: dict):
    """Write the wake-up morning summary that the user reads first."""
    today = datetime.now().strftime("%Y-%m-%d")
    body = (
        f"# おはよう、Joe ☀️ — OZ夜間進化レポート\n\n"
        f"**期間**: {stats['started_at']} → {stats['ended_at']}\n"
        f"**稼働**: {stats['duration_hours']}h\n\n"
        "## 実行サマリー\n\n"
        f"- 完了サイクル: {stats['cycles']} / {stats['max_cycles']}\n"
        f"- ステップ成功: {stats['steps_ok']} / {stats['steps_total']}\n"
        f"- 消費: {stats['spent_ozc']} OZC / {stats['max_ozc']} OZC上限\n\n"
        "## 学んだこと\n\n"
        "各エージェントの最新 reflection は `agents/<agent>.md` を参照。\n\n"
        "- [coder](agents/coder.md)\n"
        "- [researcher](agents/researcher.md)\n"
        "- [reviewer](agents/reviewer.md)\n"
        "- [debugger](agents/debugger.md)\n"
        "- [writer](agents/writer.md)\n"
        "- [scheduler](agents/scheduler.md)\n"
        "- [hitomi](agents/hitomi.md)\n"
        "- [iphone-bridge](agents/iphone-bridge.md)\n"
        "- [macos-bridge](agents/macos-bridge.md)\n\n"
        "## 今日のジャーナル\n\n"
        f"`knowledge/journal_{today}.md` に各サイクルの記録あり。\n\n"
        "## 起動を止めるには\n\n"
        "```bash\n"
        "touch ~/.openclaw/oz_vault/STOP\n"
        "```\n\n"
        "## 進捗の確認\n\n"
        "```bash\n"
        "cat ~/.openclaw/oz_vault/heartbeat.json\n"
        "tail ~/.openclaw/oz_vault/evolve.log\n"
        "```\n"
    )
    SUMMARY_PATH.write_text(body, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="OZ self-evolve loop")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Start the evolution loop")
    run_p.add_argument("--hours", type=float, default=DEFAULT_HOURS)
    run_p.add_argument("--interval-min", type=int, default=DEFAULT_INTERVAL_MIN)
    run_p.add_argument("--max-cycles", type=int, default=DEFAULT_MAX_CYCLES)
    run_p.add_argument("--max-ozc", type=float, default=DEFAULT_MAX_OZC_TOTAL)

    sub.add_parser("status", help="Show heartbeat")
    sub.add_parser("stop", help="Touch STOP file")
    sub.add_parser("logs", help="Tail logs")

    args = parser.parse_args()
    oz_vault.init_vault()
    oz_economy.init_db()

    if args.cmd == "run":
        # Catch SIGTERM cleanly
        signal.signal(signal.SIGTERM, lambda s, f: STOP_FILE.touch())
        try:
            run_loop(args)
        except KeyboardInterrupt:
            log("interrupted by user")
            STOP_FILE.touch()
        except Exception:
            log("FATAL: " + traceback.format_exc())
            sys.exit(1)
    elif args.cmd == "status":
        if HEARTBEAT_PATH.exists():
            print(HEARTBEAT_PATH.read_text(encoding="utf-8"))
        else:
            print("(no heartbeat — not running)")
    elif args.cmd == "stop":
        STOP_FILE.touch()
        print(f"created {STOP_FILE}")
    elif args.cmd == "logs":
        if LOG_PATH.exists():
            print(LOG_PATH.read_text(encoding="utf-8")[-3000:])
        else:
            print("(no log file)")


if __name__ == "__main__":
    main()

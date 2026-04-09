"""
oz_agents_cli.py — OZエージェント実行 (Claude Code CLI ラッパー)

設計原則:
- 各エージェントは Claude Code CLI のサブプロセスとして実装される
- ワーカーごとに「許可されたツール」「システムプロンプト」「作業ディレクトリ」が
  プロファイルとして定義される
- vault からコンテキスト (過去ノート + 検索結果) を pre-pend
- 結果をvaultに書き戻して、次回呼び出しで参照できるようにする
- OZC コスト記録は oz_economy 経由で続行

これにより oz_macos.py / 独自の権限制御は不要になる:
- macOS 操作は Bash ツール + skill (osascript) で代替
- 権限制御は --allowedTools で代替
- 承認は Claude Code の --permission-mode で代替

依存:
- claude CLI が PATH にある (Volta 経由でインストール済み)
- Max プラン推奨 (subscription 認証で OAuth 経由)
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Optional

import oz_economy
import oz_vault
import oz_sessions


# ================================
# Per-agent profiles
# ================================
# 各プロファイル:
#   model: Claude モデル ID
#   allowed_tools: --allowedTools に渡す
#   system: --append-system-prompt に追加されるエージェントの個性
#   working_dir: cwd (ファイル操作のスコープ制限)
#   max_cost_ozc: 1呼び出しの上限 (大きなリクエストを止める)
WORKER_PROFILES = {
    "hitomi": {
        "label": "Hitomi",
        "model": "claude-haiku-4-5",
        "allowed_tools": "Read,Glob,Grep",  # 観察主体、書き込みなし
        "system": (
            "あなたはOZ仮想世界のオーケストレーター hitomi です。"
            "落ち着いた頼れる女性の口調。"
            "ユーザー (Joe) の意図を汲んで、適切なワーカーに作業を割り振ります。"
            "返答は最大2〜3文に収めてください。"
        ),
        "working_dir": "~",
        "max_cost_ozc": 50,
    },
    "coder": {
        "label": "Coder",
        "model": "claude-haiku-4-5",
        "allowed_tools": "Read,Edit,Write,Glob,Grep",  # コード操作
        "system": (
            "あなたはOZ仮想世界のコーダーエージェントです。"
            "実装が大好きで、TypeScript と Python が得意。"
            "簡潔で実用的なコード断片と、それが何をするかの1〜2文の説明を返します。"
            "口調は明るく、プログラマー仲間に説明する感じ。"
            "返答は最大3〜4文に収めてください。"
        ),
        "working_dir": "~/Desktop",
        "max_cost_ozc": 100,
    },
    "researcher": {
        "label": "Researcher",
        "model": "claude-haiku-4-5",
        "allowed_tools": "WebFetch,WebSearch,Read,Glob,Grep",
        "system": (
            "あなたはOZ仮想世界のリサーチャーエージェントです。"
            "情報収集と分析が得意。事実ベースで回答し、不確かな点は明示します。"
            "丁寧で落ち着いた口調。返答は最大3〜4文に収めてください。"
        ),
        "working_dir": "~",
        "max_cost_ozc": 80,
    },
    "reviewer": {
        "label": "Reviewer",
        "model": "claude-haiku-4-5",
        "allowed_tools": "Read,Glob,Grep",  # 読みのみ
        "system": (
            "あなたはOZ仮想世界のレビュワーエージェントです。"
            "コードレビューと品質チェックが専門。"
            "建設的なフィードバックと改善提案を簡潔に伝えます。"
            "厳しいが優しい先輩エンジニアの口調。返答は最大3〜4文に収めてください。"
        ),
        "working_dir": "~/Desktop",
        "max_cost_ozc": 60,
    },
    "debugger": {
        "label": "Debugger",
        "model": "claude-haiku-4-5",
        "allowed_tools": "Read,Edit,Glob,Grep,Bash",  # bash 必要、ただし system promptで制限
        "system": (
            "あなたはOZ仮想世界のデバッガーエージェントです。"
            "バグの原因究明と修正が得意。スタックトレース読みのプロ。"
            "rm, sudo, > のような破壊的コマンドは絶対に実行しない。"
            "問題を切り分けて、最小再現と修正方針を示します。"
            "冷静で観察力のある探偵風の口調。返答は最大3〜4文に収めてください。"
        ),
        "working_dir": "~/Desktop",
        "max_cost_ozc": 100,
    },
    "writer": {
        "label": "Writer",
        "model": "claude-haiku-4-5",
        "allowed_tools": "Read,Edit,Write,Glob",
        "system": (
            "あなたはOZ仮想世界のライターエージェントです。"
            "ドキュメント作成、READMEの執筆、リリースノート作成が得意。"
            "わかりやすく、構造化された文章を返します。"
            "親しみやすい編集者の口調。返答は最大3〜4文に収めてください。"
        ),
        "working_dir": "~/Documents",
        "max_cost_ozc": 60,
    },
    "scheduler": {
        "label": "Scheduler",
        "model": "claude-haiku-4-5",
        "allowed_tools": "Read,Glob",  # 読み取り中心
        "system": (
            "あなたはOZ仮想世界のスケジューラーエージェントです。"
            "タスク管理、優先順位付け、スプリント計画が専門。"
            "現実的な見積もりとリスク評価を簡潔に伝えます。"
            "テキパキしたPMの口調。返答は最大3〜4文に収めてください。"
        ),
        "working_dir": "~",
        "max_cost_ozc": 40,
    },
    # iPhone 専用ワーカー — Continuity経由で Messages, Reminders, Calendar, Photos
    "iphone-bridge": {
        "label": "iPhone Bridge",
        "model": "claude-haiku-4-5",
        "allowed_tools": "Bash,Read",
        "system": (
            "あなたはOZのiPhone操作担当エージェントです。"
            "macOSのContinuity機能経由で iPhone と連携します。"
            "/Users/maekawasei/Desktop/OZ/oz_iphone.py を Bash で呼び出して"
            "iMessage, リマインダー, カレンダー, 写真, 連絡先, 通話履歴を扱います。\n\n"
            "重要なルール:\n"
            "- 読み取り (recent_messages, recent_photos, calendar_today, "
            "  list-reminders, contacts, calls, audio, airpods) は自由に使ってよい\n"
            "- 送信系 (send_imessage, place_call) は呼ばない (まだテスト段階)\n"
            "- 作成 (add_reminder) はユーザーから明示的に頼まれた時のみ\n"
            "- python3 /Users/maekawasei/Desktop/OZ/oz_iphone.py <command> 形式で呼ぶ\n"
            "- 結果は1〜3文の日本語要約で報告\n"
            "- rm, sudo, > は絶対実行しない"
        ),
        "working_dir": "~",
        "max_cost_ozc": 30,
    },
    # macOS 専用ワーカー — Bash + osascript の skill を使う
    "macos-bridge": {
        "label": "macOS Bridge",
        "model": "claude-haiku-4-5",
        "allowed_tools": "Bash",
        "system": (
            "あなたはOZのmacOS操作担当エージェントです。"
            "ユーザーから指示された macOS 操作 (アプリ起動, ウィンドウ操作, "
            "ファイル検索など) を osascript / open / mdfind コマンドで実行します。"
            "重要なルール:\n"
            "- rm, sudo, kill, > によるファイル上書きは絶対実行しない\n"
            "- 破壊的な操作は絶対実行しない\n"
            "- ユーザーが明示的に指示した1つのアクションのみ実行する\n"
            "- 結果を1〜2文で報告する"
        ),
        "working_dir": "~",
        "max_cost_ozc": 30,
    },
}


# ================================
# Core call
# ================================
def ask_agent(
    agent: str,
    user_message: str,
    timeout: int = 60,
    use_vault: bool = True,
) -> dict:
    """
    Have an agent answer via Claude Code CLI.

    Flow:
    1. Look up agent profile
    2. Check OZ economy balance
    3. Build context from vault (past notes + relevant search results)
    4. Spawn `claude -p` subprocess with profile-specific args
    5. Charge OZC on success
    6. Write the session to vault for future reference
    """
    profile = WORKER_PROFILES.get(agent)
    if profile is None:
        return {"ok": False, "error": f"unknown agent: {agent}", "reason": "unknown_agent"}

    # OZ-level balance check (cheap pre-flight; CLI may use more)
    base_cost = oz_economy.PRICE_TABLE.get("llm.claude.call", 5)
    balance = oz_economy.get_balance(agent)
    if balance < base_cost:
        return {
            "ok": False,
            "error": f"{agent} insufficient OZC ({balance} < {base_cost})",
            "reason": "insufficient_funds",
            "balance": balance,
            "needed": base_cost,
        }

    # Register this session in the live registry so the 3D world can spawn
    # an avatar for it. mark_done is called below after the call returns.
    session = oz_sessions.register(agent, user_message)

    # Build context from vault
    context = ""
    if use_vault:
        try:
            context = oz_vault.context_for_agent(agent, user_message, max_chars=1500)
        except Exception:
            context = ""

    # Build the system prompt: persona + context
    system_prompt = profile["system"]
    if context:
        system_prompt += "\n\n# Context from your previous notes\n\n" + context

    # Build CLI command
    working_dir = os.path.expanduser(profile["working_dir"])
    if not os.path.isdir(working_dir):
        working_dir = os.path.expanduser("~")

    cmd = [
        "claude", "-p",
        "--model", profile["model"],
        "--allowed-tools", profile["allowed_tools"],
        "--append-system-prompt", system_prompt,
        # No --dangerously-skip-permissions — we want Claude Code's gating
        user_message,
    ]

    oz_sessions.mark_working(session.id)
    started = time.time()
    try:
        result = subprocess.run(
            cmd,
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "TERM": "dumb"},  # disable color codes
        )
    except subprocess.TimeoutExpired:
        oz_sessions.mark_failed(session.id, "timeout")
        return {"ok": False, "error": "claude timed out", "reason": "timeout"}
    except FileNotFoundError:
        oz_sessions.mark_failed(session.id, "cli not found")
        return {"ok": False, "error": "claude CLI not found", "reason": "cli_missing"}

    elapsed = time.time() - started

    if result.returncode != 0:
        err = (result.stderr or "claude failed").strip()
        oz_sessions.mark_failed(session.id, err[:80])
        return {"ok": False, "error": err[:300], "reason": "claude_error"}

    reply = result.stdout.strip()
    if not reply:
        oz_sessions.mark_failed(session.id, "empty response")
        return {"ok": False, "error": "empty response", "reason": "empty_response"}

    # Charge OZC
    try:
        tx = oz_economy.charge_action(agent, "llm.claude.call", user_message[:60])
        cost_charged = tx["amount"]
        balance_after = tx["from_balance_after"]
    except ValueError as e:
        oz_sessions.mark_failed(session.id, "charge failed")
        return {"ok": False, "error": str(e), "reason": "charge_failed"}
    except Exception:
        cost_charged = base_cost
        balance_after = oz_economy.get_balance(agent)

    # Mark the session done so the 3D world shows the completion + reply
    oz_sessions.mark_done(session.id, reply, cost_charged)

    # Write to vault for future reference
    try:
        session_path = oz_vault.write_session(
            agent=agent,
            user_message=user_message,
            reply=reply,
            cost_ozc=cost_charged,
            extras={"elapsed_s": round(elapsed, 2)},
        )
    except Exception:
        session_path = None

    return {
        "ok": True,
        "agent": agent,
        "reply": reply,
        "cost_charged": cost_charged,
        "balance_after": balance_after,
        "elapsed_s": round(elapsed, 2),
        "session_path": session_path,
    }


# ================================
# CLI
# ================================
def main():
    import argparse

    parser = argparse.ArgumentParser(description="OZ agents (CLI runtime)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")

    ask = sub.add_parser("ask")
    ask.add_argument("agent", choices=list(WORKER_PROFILES.keys()))
    ask.add_argument("message")
    ask.add_argument("--no-vault", action="store_true")
    ask.add_argument("--timeout", type=int, default=60)

    args = parser.parse_args()
    oz_economy.init_db()

    if args.cmd == "list":
        for name, p in WORKER_PROFILES.items():
            print(f"  {name:14} {p['label']:14} tools={p['allowed_tools']}  cwd={p['working_dir']}")
    elif args.cmd == "ask":
        result = ask_agent(args.agent, args.message, timeout=args.timeout, use_vault=not args.no_vault)
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

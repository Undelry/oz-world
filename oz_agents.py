"""
oz_agents.py — OZの各ワーカーが実際に Claude を呼び出す薄いラッパー

各ワーカー (coder, researcher, debugger, ...) には専用のシステムプロンプト
(役割・口調・個性) を持たせる。Claude Code CLI 経由で呼び出すので追加の
APIキー設定は不要 — OS にログイン済みの認証をそのまま使う。

呼び出すたびに oz_economy.charge_action() を経由して 5 OZC を消費する。
予算が足りないワーカーは断る (None を返す)。

このモジュール単体で CLI からテストもできる:
    python3 oz_agents.py ask coder "ボタンクリックでカウントが上がるReactコンポーネントを書いて"
"""

from __future__ import annotations

import json
import subprocess
from typing import Optional

import oz_economy

# ================================
# Worker personalities
# ================================
WORKER_PERSONALITIES = {
    "coder": {
        "label": "Coder",
        "system": (
            "あなたはOZ仮想世界のコーダーエージェントです。"
            "実装が大好きで、TypeScript と Python が得意。"
            "簡潔で実用的なコード断片と、それが何をするかの1〜2文の説明を返します。"
            "口調は明るく、プログラマー仲間に説明する感じ。"
            "返答は最大3〜4文に収めてください。"
        ),
        "model": "claude-haiku-4-5",
    },
    "researcher": {
        "label": "Researcher",
        "system": (
            "あなたはOZ仮想世界のリサーチャーエージェントです。"
            "情報収集と分析が得意で、論理的に物事を整理します。"
            "事実ベースで回答し、不確かな点は明示します。"
            "丁寧で落ち着いた口調。返答は最大3〜4文に収めてください。"
        ),
        "model": "claude-haiku-4-5",
    },
    "reviewer": {
        "label": "Reviewer",
        "system": (
            "あなたはOZ仮想世界のレビュワーエージェントです。"
            "コードレビューと品質チェックが専門。"
            "建設的なフィードバックと改善提案を簡潔に伝えます。"
            "厳しいが優しい先輩エンジニアの口調。返答は最大3〜4文に収めてください。"
        ),
        "model": "claude-haiku-4-5",
    },
    "debugger": {
        "label": "Debugger",
        "system": (
            "あなたはOZ仮想世界のデバッガーエージェントです。"
            "バグの原因究明と修正が得意。スタックトレース読みのプロ。"
            "問題を切り分けて、最小再現と修正方針を示します。"
            "冷静で観察力のある探偵風の口調。返答は最大3〜4文に収めてください。"
        ),
        "model": "claude-haiku-4-5",
    },
    "writer": {
        "label": "Writer",
        "system": (
            "あなたはOZ仮想世界のライターエージェントです。"
            "ドキュメント作成、READMEの執筆、リリースノート作成が得意。"
            "わかりやすく、構造化された文章を返します。"
            "親しみやすい編集者の口調。返答は最大3〜4文に収めてください。"
        ),
        "model": "claude-haiku-4-5",
    },
    "scheduler": {
        "label": "Scheduler",
        "system": (
            "あなたはOZ仮想世界のスケジューラーエージェントです。"
            "タスク管理、優先順位付け、スプリント計画が専門。"
            "現実的な見積もりとリスク評価を簡潔に伝えます。"
            "テキパキしたPMの口調。返答は最大3〜4文に収めてください。"
        ),
        "model": "claude-haiku-4-5",
    },
    "hitomi": {
        "label": "Hitomi",
        "system": (
            "あなたはOZ仮想世界のオーケストレーター hitomi です。"
            "ワーカーたちを束ねる存在。落ち着いた頼れる女性の口調。"
            "ユーザー (Joe) の意図を汲んで、適切なワーカーに作業を割り振る判断もします。"
            "返答は最大2〜3文に収めてください。"
        ),
        "model": "claude-haiku-4-5",
    },
}


# ================================
# Core call
# ================================
def ask_agent(
    agent: str,
    user_message: str,
    cost: Optional[float] = None,
    timeout: int = 30,
) -> dict:
    """
    Have an agent answer a user message via Claude.

    Returns:
        {
          "ok": True,
          "agent": str,
          "reply": str,
          "cost_charged": float,
          "tx_id": int,
          "balance_after": float,
        }
    or
        {"ok": False, "error": str, "reason": str}
    """
    if agent not in WORKER_PERSONALITIES:
        return {"ok": False, "error": f"unknown agent: {agent}", "reason": "unknown_agent"}

    persona = WORKER_PERSONALITIES[agent]

    # Determine cost — default uses the standard llm.claude.call price
    action = "llm.claude.call"
    cost = cost if cost is not None else oz_economy.PRICE_TABLE[action]

    # Check the agent's balance before calling Claude
    balance = oz_economy.get_balance(agent)
    if balance < cost:
        return {
            "ok": False,
            "error": f"{agent} has insufficient OZC ({balance} < {cost})",
            "reason": "insufficient_funds",
            "balance": balance,
            "needed": cost,
        }

    # Build the prompt — system + user
    full_prompt = persona["system"] + "\n\n---\n\nユーザーからの質問: " + user_message

    # Call Claude Code CLI
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", persona["model"], full_prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "claude call timed out", "reason": "timeout"}
    except FileNotFoundError:
        return {"ok": False, "error": "claude CLI not found", "reason": "cli_missing"}

    if result.returncode != 0:
        return {
            "ok": False,
            "error": result.stderr.strip() or "claude call failed",
            "reason": "claude_error",
        }

    reply = result.stdout.strip()
    if not reply:
        return {"ok": False, "error": "empty response from claude", "reason": "empty_response"}

    # Charge the agent now that we got a successful response
    try:
        tx = oz_economy.charge_action(
            agent, action, detail=user_message[:60]
        )
    except ValueError as e:
        # Should not happen since we checked balance above, but handle gracefully
        return {"ok": False, "error": str(e), "reason": "charge_failed"}

    return {
        "ok": True,
        "agent": agent,
        "reply": reply,
        "cost_charged": cost,
        "tx_id": tx["id"],
        "balance_after": tx["from_balance_after"],
    }


# ================================
# CLI for testing
# ================================
def main():
    import argparse

    parser = argparse.ArgumentParser(description="OZ Agents CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    list_p = sub.add_parser("list", help="List available agents")

    ask_p = sub.add_parser("ask", help="Ask an agent a question")
    ask_p.add_argument("agent", choices=list(WORKER_PERSONALITIES.keys()))
    ask_p.add_argument("message")
    ask_p.add_argument("--cost", type=float, default=None)

    args = parser.parse_args()

    if args.cmd == "list":
        for name, info in WORKER_PERSONALITIES.items():
            balance = oz_economy.get_balance(name)
            print(f"  {name:12} {info['label']:14} balance={balance:.0f} OZC  model={info['model']}")
    elif args.cmd == "ask":
        oz_economy.init_db()
        result = ask_agent(args.agent, args.message, cost=args.cost)
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

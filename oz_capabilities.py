"""
oz_capabilities.py — OZエージェントの権限 (capability) 管理

OZの設計原則:
- お金 (OZC) と 権限 (capability) は別物
- お金が払えるかと、その行為をやっていいかは独立にチェック
- エージェントは「最小権限の原則」に従って必要最小限のcapabilityだけ持つ

capability の3段階:
- "always"       — 自由に実行可能
- "user-approve" — 1回ごとにユーザー承認が必要
- "deny"         — 絶対に実行できない (デフォルト)

未定義のアクションは常に deny。エージェントが新しい行為をするには
ホワイトリストへの追加が必須。
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class Permission(str, Enum):
    ALWAYS = "always"
    USER_APPROVE = "user-approve"
    DENY = "deny"


# ================================
# Per-agent capability registry
# ================================
# 設計のポイント:
# - 全エージェントはデフォルトで何もできない (空 dict = 全 deny)
# - 必要なものだけ明示的に与える
# - 危険な操作 (file.delete, shell.exec, external.http) は "user-approve" 以上にする
# - LLMやTTSなど無害なものだけ "always"
WORKER_CAPABILITIES: dict[str, dict[str, Permission]] = {
    "hitomi": {
        # オーケストレーター。LLM呼び出しと TTS は自由
        "llm.claude":      Permission.ALWAYS,
        "tts.speak":       Permission.ALWAYS,
        # 内部経済操作 (お金の移動) は自由
        "economy.transfer": Permission.ALWAYS,
        "economy.assign":   Permission.ALWAYS,
        # ワーカーへの指示は自由
        "agent.dispatch":   Permission.ALWAYS,
        # 危険な操作はユーザー承認
        "file.write":       Permission.USER_APPROVE,
        "external.http":    Permission.USER_APPROVE,
        "app.launch":       Permission.USER_APPROVE,
        # 完全に禁止
        "file.delete":      Permission.DENY,
        "shell.exec":       Permission.DENY,
    },
    "coder": {
        "llm.claude":      Permission.ALWAYS,
        "tts.speak":       Permission.ALWAYS,
        "code.read":       Permission.ALWAYS,
        "code.write":      Permission.ALWAYS,
        "code.delete":     Permission.USER_APPROVE,
        # コード書きは外部通信不要 — プロンプト注入で外部に送信される経路を遮断
        "external.http":   Permission.DENY,
        "shell.exec":      Permission.DENY,
        "file.delete":     Permission.DENY,
    },
    "researcher": {
        "llm.claude":      Permission.ALWAYS,
        "tts.speak":       Permission.ALWAYS,
        "external.http":   Permission.ALWAYS,  # 検索のため必須
        "file.read":       Permission.ALWAYS,
        # リサーチャはコードを書かない・修正しない
        "code.write":      Permission.DENY,
        "code.delete":     Permission.DENY,
        "shell.exec":      Permission.DENY,
        "file.delete":     Permission.DENY,
    },
    "reviewer": {
        "llm.claude":      Permission.ALWAYS,
        "tts.speak":       Permission.ALWAYS,
        "code.read":       Permission.ALWAYS,
        # レビュワは読むだけ。書き換え・削除はしない
        "code.write":      Permission.DENY,
        "code.delete":     Permission.DENY,
        "external.http":   Permission.DENY,
        "shell.exec":      Permission.DENY,
        "file.delete":     Permission.DENY,
    },
    "debugger": {
        "llm.claude":      Permission.ALWAYS,
        "tts.speak":       Permission.ALWAYS,
        "code.read":       Permission.ALWAYS,
        "code.write":      Permission.USER_APPROVE,
        # スタックトレース実行のため shell が必要 — ただし毎回承認
        "shell.exec":      Permission.USER_APPROVE,
        "code.delete":     Permission.DENY,
        "external.http":   Permission.DENY,
        "file.delete":     Permission.DENY,
    },
    "writer": {
        "llm.claude":      Permission.ALWAYS,
        "tts.speak":       Permission.ALWAYS,
        "file.read":       Permission.ALWAYS,
        "file.write":      Permission.USER_APPROVE,
        # ライターはコード触らない
        "code.write":      Permission.DENY,
        "code.delete":     Permission.DENY,
        "external.http":   Permission.DENY,
        "shell.exec":      Permission.DENY,
        "file.delete":     Permission.DENY,
    },
    "scheduler": {
        "llm.claude":      Permission.ALWAYS,
        "tts.speak":       Permission.ALWAYS,
        "calendar.read":   Permission.ALWAYS,
        "calendar.write":  Permission.USER_APPROVE,
        # スケジューラはコードもファイルも触らない
        "code.write":      Permission.DENY,
        "file.write":      Permission.DENY,
        "external.http":   Permission.DENY,
        "shell.exec":      Permission.DENY,
        "file.delete":     Permission.DENY,
    },
    # 外部AIプロバイダー (ゲストエージェント) は最小権限
    # 入札に勝ってOZに招待されたら、LLM呼び出しだけ許可される
    # (ファイル・コード・外部通信などは一切不可)
    "claude-direct": {
        "llm.claude":      Permission.ALWAYS,
    },
    "gpt-4o": {
        "llm.claude":      Permission.ALWAYS,
    },
    "gemini-pro": {
        "llm.claude":      Permission.ALWAYS,
    },
    "llama-local": {
        "llm.claude":      Permission.ALWAYS,
    },
}


def get_permission(agent: str, action: str) -> Permission:
    """
    Look up the permission level for a specific agent + action pair.

    Returns Permission.DENY for any agent/action that isn't explicitly listed.
    This is the *closed-world* default — anything we haven't whitelisted is
    forbidden, full stop.
    """
    caps = WORKER_CAPABILITIES.get(agent)
    if caps is None:
        return Permission.DENY
    return caps.get(action, Permission.DENY)


def can_execute(agent: str, action: str) -> bool:
    """Quick check: is this action allowed at all (with or without approval)?"""
    return get_permission(agent, action) != Permission.DENY


def needs_approval(agent: str, action: str) -> bool:
    """Does this action require interactive user approval?"""
    return get_permission(agent, action) == Permission.USER_APPROVE


def list_capabilities(agent: str) -> dict:
    """Return a dict view of an agent's capabilities (string values for JSON)."""
    caps = WORKER_CAPABILITIES.get(agent, {})
    return {action: perm.value for action, perm in caps.items()}


def all_agents() -> list[str]:
    return list(WORKER_CAPABILITIES.keys())


def main():
    """CLI: list capabilities for inspection."""
    import json
    import argparse

    parser = argparse.ArgumentParser(description="OZ Capabilities Inspector")
    parser.add_argument("agent", nargs="?", help="agent name (omit to see all)")
    args = parser.parse_args()

    if args.agent:
        caps = list_capabilities(args.agent)
        if not caps:
            print(f"unknown agent: {args.agent}")
        else:
            print(json.dumps({args.agent: caps}, indent=2, ensure_ascii=False))
    else:
        out = {a: list_capabilities(a) for a in all_agents()}
        print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

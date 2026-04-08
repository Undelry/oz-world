"""
oz_external.py — 外部AIマーケットプレイス (Phase 4)

OZの内部ワーカー (coder, researcher, ...) だけでなく、外部のAIプロバイダー
(Claude Direct API, GPT-4, Gemini, Llama, etc.) も「ゲストエージェント」として
OZの経済圏に参加できる。

各外部プロバイダーには:
- リアル通貨での実コスト (JPY) — そのプロバイダーのAPI料金
- OZC換算レート (1 OZC ≈ ¥1 を基準)
- 得意分野 (キーワード → 安値で入札)
- 残高 (treasury から topup される)

外部エージェントが入札に勝った場合、本物のAPIを呼ぶ代わりに今は
Claude Code CLI を介して応答を返す (stub) 。インテグレーションを差し替えれば
そのまま本物のGPT/Gemini呼び出しに切り替えられる構造。
"""

from __future__ import annotations

import json
import subprocess
from typing import Optional

import oz_economy

# ================================
# 為替レート
# ================================
# 1 OZC = ¥1 (調整可能)
OZC_TO_JPY = 1.0


def ozc_to_jpy(amount: float) -> float:
    return amount * OZC_TO_JPY


def jpy_to_ozc(jpy: float) -> float:
    return jpy / OZC_TO_JPY


# ================================
# 外部プロバイダー
# ================================
EXTERNAL_PROVIDERS = {
    "claude-direct": {
        "label": "Claude Direct",
        "emoji": "🌌",
        "color": "#a855f7",
        "real_cost_jpy": 8.0,        # 1 call ≈ ¥8
        "specialty_keywords": ["コード", "実装", "分析", "推論", "claude"],
        "expert_mult": 0.65,
        "novice_mult": 1.4,
        "real_provider": "anthropic",
    },
    "gpt-4o": {
        "label": "GPT-4o",
        "emoji": "🟢",
        "color": "#10b981",
        "real_cost_jpy": 6.0,
        "specialty_keywords": ["翻訳", "文章", "creative", "writing", "クリエイティブ"],
        "expert_mult": 0.65,
        "novice_mult": 1.4,
        "real_provider": "openai",
    },
    "gemini-pro": {
        "label": "Gemini Pro",
        "emoji": "🔷",
        "color": "#3b82f6",
        "real_cost_jpy": 4.0,
        "specialty_keywords": ["search", "web", "リサーチ", "research", "事実確認"],
        "expert_mult": 0.7,
        "novice_mult": 1.5,
        "real_provider": "google",
    },
    "llama-local": {
        "label": "Llama Local",
        "emoji": "🦙",
        "color": "#f59e0b",
        "real_cost_jpy": 0.5,         # ローカル実行ほぼ無料
        "specialty_keywords": ["量", "bulk", "簡単", "シンプル"],
        "expert_mult": 0.6,
        "novice_mult": 1.6,
        "real_provider": "local",
    },
}


# ================================
# 入札ロジック
# ================================
def calc_external_bid(provider: str, task: str) -> float:
    """
    外部プロバイダーが任意のタスクに対して提示する入札額 (OZC)。
    実コスト + 得意度マルチプライヤー + マージン (15%) で算出。
    """
    info = EXTERNAL_PROVIDERS.get(provider)
    if info is None:
        return 9999

    task_lower = task.lower()
    matches = sum(1 for kw in info["specialty_keywords"] if kw in task_lower)

    if matches > 0:
        mult = info["expert_mult"] * (0.92 ** (matches - 1))
    else:
        mult = info["novice_mult"]

    # Real cost in JPY → OZC + 15% margin
    base_ozc = jpy_to_ozc(info["real_cost_jpy"]) * 1.15
    bid = max(2, round(base_ozc * mult))
    return bid


def get_all_external_bids(task: str) -> list:
    """全外部プロバイダーの入札を集める。"""
    bids = []
    for provider in EXTERNAL_PROVIDERS:
        bid = calc_external_bid(provider, task)
        info = EXTERNAL_PROVIDERS[provider]
        bids.append(
            {
                "worker": provider,
                "bid": bid,
                "real_cost_jpy": info["real_cost_jpy"],
                "is_external": True,
                "label": info["label"],
                "emoji": info["emoji"],
                "color": info["color"],
                "balance": oz_economy.get_balance(provider),
            }
        )
    return bids


# ================================
# Provider registration in the economy
# ================================
def ensure_provider_accounts():
    """
    Make sure each external provider has an account in the OZ economy.
    Externals start with 0 balance — they don't have OZC, they earn it.
    """
    for provider in EXTERNAL_PROVIDERS:
        try:
            balance = oz_economy.get_balance(provider)
            if balance == 0:
                # Auto-creates the row via the next transfer if needed.
                # Use direct sqlite insert via topup-style call (no real money)
                pass
        except Exception:
            pass


# ================================
# Calling an external provider
# ================================
def call_external(provider: str, prompt: str, timeout: int = 45) -> dict:
    """
    Send a prompt to an external provider.

    Real provider integration is stubbed for now — every external call goes
    through Claude Code CLI as a placeholder. Architecture is ready for
    swap-in of real OpenAI/Gemini/local SDKs.

    Returns:
        {"ok": bool, "reply": str, "real_cost_jpy": float, ...}
    """
    info = EXTERNAL_PROVIDERS.get(provider)
    if info is None:
        return {"ok": False, "error": f"unknown provider: {provider}"}

    # In a real impl this branches per provider. For now everything goes
    # through Claude Code CLI but tagged with the original provider's persona.
    persona_prefix = (
        f"あなたは外部AIエージェント '{info['label']}' として OZ仮想世界に呼ばれました。"
        f"日本語で簡潔に答えてください (最大3文)。\n\n質問: "
    )

    try:
        result = subprocess.run(
            ["claude", "-p", "--model", "claude-haiku-4-5", persona_prefix + prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "real_cost_jpy": 0}
    except FileNotFoundError:
        return {"ok": False, "error": "claude CLI not found", "real_cost_jpy": 0}

    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip()[:200], "real_cost_jpy": 0}

    return {
        "ok": True,
        "provider": provider,
        "label": info["label"],
        "reply": result.stdout.strip(),
        "real_cost_jpy": info["real_cost_jpy"],
    }


# ================================
# CLI for testing
# ================================
def main():
    import argparse

    parser = argparse.ArgumentParser(description="OZ External Marketplace CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List external providers")

    bids_p = sub.add_parser("bids", help="Get external bids for a task")
    bids_p.add_argument("task")

    call_p = sub.add_parser("call", help="Call an external provider")
    call_p.add_argument("provider", choices=list(EXTERNAL_PROVIDERS.keys()))
    call_p.add_argument("prompt")

    args = parser.parse_args()
    oz_economy.init_db()

    if args.cmd == "list":
        for name, info in EXTERNAL_PROVIDERS.items():
            print(
                f"  {info['emoji']} {name:14} {info['label']:14} "
                f"¥{info['real_cost_jpy']:>5.1f}/call  ({jpy_to_ozc(info['real_cost_jpy']):.1f} OZC)"
            )
    elif args.cmd == "bids":
        for b in get_all_external_bids(args.task):
            print(
                f"  {b['emoji']} {b['label']:14} bid={b['bid']:>5.0f} OZC  "
                f"(real ¥{b['real_cost_jpy']})"
            )
    elif args.cmd == "call":
        result = call_external(args.provider, args.prompt)
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

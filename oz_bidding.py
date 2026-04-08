"""
oz_bidding.py — ワーカー入札制 (Phase AD)

タスクを開いたとき、各ワーカーが「自分はこの値段でやります」と
入札する。最安値を提示したワーカーが落札 → hitomi が予算を渡す。

入札の決め方は単純で、各ワーカーの "強み" に応じた基本料金 + ランダム要素。
本物のClaudeを呼ぶと遅いので、入札はローカルロジックだけで瞬時に解決する。

タスクのラベルから「どのワーカーに向いているか」をキーワードマッチで判定し、
得意なワーカーは安値・不得意なワーカーは高値を提示する。
"""

from __future__ import annotations

import random
import time
from typing import Optional

import oz_economy

# ================================
# Worker specialties — keyword → cost multiplier
# ================================
# 値が小さいほどそのワーカーは「得意」 (= 安く請け負う)
WORKER_SPECIALTIES = {
    "coder": {
        "keywords": ["コード", "実装", "api", "react", "typescript", "python", "build", "プログラ"],
        "base": 30,
        "expert_mult": 0.7,
        "novice_mult": 1.5,
    },
    "researcher": {
        "keywords": ["調査", "リサーチ", "research", "分析", "データ", "情報", "調べ"],
        "base": 25,
        "expert_mult": 0.7,
        "novice_mult": 1.5,
    },
    "reviewer": {
        "keywords": ["レビュー", "review", "pr", "チェック", "確認", "監査", "品質"],
        "base": 20,
        "expert_mult": 0.7,
        "novice_mult": 1.5,
    },
    "debugger": {
        "keywords": ["バグ", "bug", "デバッグ", "debug", "エラー", "クラッシュ", "fix", "修正"],
        "base": 35,
        "expert_mult": 0.7,
        "novice_mult": 1.5,
    },
    "writer": {
        "keywords": ["ドキュメント", "doc", "readme", "文章", "解説", "リリース", "ノート", "翻訳"],
        "base": 22,
        "expert_mult": 0.7,
        "novice_mult": 1.5,
    },
    "scheduler": {
        "keywords": ["スケジュール", "計画", "plan", "見積", "タスク管理", "スプリント", "優先"],
        "base": 18,
        "expert_mult": 0.7,
        "novice_mult": 1.5,
    },
}


def calc_bid(worker: str, task: str, current_balance: float) -> float:
    """
    Compute the OZC amount this worker would bid for the given task.
    Lower bid = more interest. None of these workers can bid below 5 OZC
    (you can't quote less than the base llm.claude.call cost).
    """
    info = WORKER_SPECIALTIES.get(worker)
    if info is None:
        return 999

    task_lower = task.lower()
    matches = sum(1 for kw in info["keywords"] if kw in task_lower)

    if matches > 0:
        # Expert in this area — bid lower
        mult = info["expert_mult"] * (0.9 ** (matches - 1))
    else:
        # Not their specialty — bid higher
        mult = info["novice_mult"]

    # Hungry workers (low balance) bid lower to secure work
    if current_balance < 100:
        mult *= 0.85
    elif current_balance > 1000:
        mult *= 1.15

    # Add a tiny bit of randomness so bids aren't deterministic
    mult *= 0.9 + random.random() * 0.2

    bid = max(5, round(info["base"] * mult))
    return bid


def collect_bids(task: str) -> list:
    """
    Ask every worker to bid on a task.

    Returns a sorted list of dicts: [{worker, bid, balance}], cheapest first.
    """
    bids = []
    for worker in WORKER_SPECIALTIES:
        balance = oz_economy.get_balance(worker)
        bid = calc_bid(worker, task, balance)
        bids.append({"worker": worker, "bid": bid, "balance": balance})

    bids.sort(key=lambda b: b["bid"])
    return bids


def run_auction(task: str, max_budget: Optional[float] = None) -> dict:
    """
    Run the full auction flow:
      1. Collect bids
      2. Pick the cheapest
      3. hitomi transfers the bid amount to the winner
      4. Return the auction record

    Args:
        task: human-readable task description
        max_budget: hitomi will not pay more than this (rejects auction if too expensive)
    """
    bids = collect_bids(task)
    if not bids:
        return {"ok": False, "error": "no workers available"}

    winner = bids[0]
    if max_budget is not None and winner["bid"] > max_budget:
        return {
            "ok": False,
            "error": f"all bids exceed budget {max_budget}",
            "bids": bids,
        }

    # hitomi pays the winner
    try:
        tx = oz_economy.transfer(
            "hitomi",
            winner["worker"],
            winner["bid"],
            "auction.win",
            task[:60],
        )
    except ValueError as e:
        return {"ok": False, "error": str(e), "reason": "transfer_failed"}

    return {
        "ok": True,
        "task": task,
        "bids": bids,
        "winner": winner["worker"],
        "winning_bid": winner["bid"],
        "tx_id": tx["id"],
        "ts": time.time(),
    }


# ================================
# CLI for testing
# ================================
def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(description="OZ Bidding CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    bids_p = sub.add_parser("bids", help="Show bids for a task")
    bids_p.add_argument("task")

    auction_p = sub.add_parser("auction", help="Run an auction for a task")
    auction_p.add_argument("task")
    auction_p.add_argument("--budget", type=float, default=None)

    args = parser.parse_args()
    oz_economy.init_db()

    if args.cmd == "bids":
        for b in collect_bids(args.task):
            print(f"  {b['worker']:12} bid={b['bid']:>6.0f} OZC  balance={b['balance']:.0f}")
    elif args.cmd == "auction":
        result = run_auction(args.task, max_budget=args.budget)
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

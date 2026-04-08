"""
oz_reflect.py — 自己改善ループ

定期的に各エージェントの最近のセッションを読み返して、
学んだこと・パターン・ユーザーの好みを抽出し、agent profile と
knowledge ノートに書き戻す。

つまり: agent が自分の過去の発言から学ぶ。

実行モデル:
- 手動: `python3 oz_reflect.py run --agent coder`
- 定期: cron や launchd で 1日1回 (例: 毎晩2時)

各 reflection は:
1. vault から指定 agent の最近 N セッションを読む
2. claude CLI を通して「この会話履歴から3つの学びを抽出」と問う
3. 結果を agent profile の `## Learnings` セクションに append
4. トピック (frequently asked) があれば knowledge/ にも書く

これにより agent は「使えば使うほど Joe を理解する」状態になる。
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import oz_economy
import oz_vault


REFLECT_PROMPT = """以下は OZ の {agent} エージェントが最近 Joe と交わしたセッションです。
これらを読んで、次の3つを簡潔に書き出してください:

1. **Joe の好み・癖** (口調、要求の出し方、優先する観点)
2. **{agent} が改善すべき点** (より良く答えるために)
3. **頻出トピック** (繰り返し聞かれていること)

回答は markdown で、3つのセクションだけ。各セクション3行以内。

# 最近のセッション

{sessions}
"""


def reflect_agent(agent: str, max_sessions: int = 10, model: str = "claude-haiku-4-5") -> dict:
    """
    Run a reflection cycle for a single agent.

    Returns a structured result describing what was learned and where it
    was written.
    """
    oz_vault.init_vault()

    sessions = oz_vault.list_recent_sessions(agent=agent, limit=max_sessions)
    if not sessions:
        return {"ok": False, "error": f"no sessions for {agent}"}

    # Stitch sessions together as plain text
    blocks = []
    for s in sessions:
        path = Path(s["path"])
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        blocks.append(f"### {s['ts']}\n{text}\n")
    if not blocks:
        return {"ok": False, "error": "could not read any sessions"}

    sessions_blob = "\n---\n".join(blocks)
    prompt = REFLECT_PROMPT.format(agent=agent, sessions=sessions_blob)

    # Call Claude (no tools — pure reflection on the conversation history).
    # NOTE: --disallowed-tools uses nargs+ so it will eat positional args after it.
    # We use --append-system-prompt as a separator so the prompt is the last
    # unambiguous positional.
    try:
        result = subprocess.run(
            [
                "claude", "-p", "--model", model,
                "--disallowed-tools", "Bash,Edit,Write,WebFetch,WebSearch",
                "--append-system-prompt", "Reflection mode: do not use any tools, just reason from the provided history.",
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "claude timed out"}
    except FileNotFoundError:
        return {"ok": False, "error": "claude CLI not found"}

    if result.returncode != 0:
        return {"ok": False, "error": result.stderr.strip()[:300]}

    learnings = result.stdout.strip()
    if not learnings:
        return {"ok": False, "error": "empty reflection"}

    # Charge OZC to the agent itself for the reflection cost
    try:
        oz_economy.charge_action(agent, "llm.claude.call", "reflection cycle")
    except Exception:
        pass

    # Append to agent profile
    profile = oz_vault.read_agent_profile(agent) or f"# {agent}\n\n"
    if "## Learnings" not in profile:
        profile += "\n## Learnings\n"
    timestamp = sessions[0]["ts"][:10]
    profile += f"\n### Reflection {timestamp}\n\n{learnings}\n"
    oz_vault.write_agent_profile(agent, profile)

    # Also write a topic-style knowledge note for cross-agent search
    oz_vault.write_knowledge(
        topic=f"reflection_{agent}_{timestamp}",
        content=f"# Reflection: {agent} ({timestamp})\n\nBased on {len(sessions)} sessions.\n\n{learnings}",
        tags=["reflection", agent],
    )

    return {
        "ok": True,
        "agent": agent,
        "sessions_analyzed": len(sessions),
        "learnings": learnings,
    }


def reflect_all(max_sessions: int = 10) -> dict:
    """Run reflection for every agent that has sessions."""
    oz_vault.init_vault()
    results = {}
    # Get unique agents that have sessions
    seen_agents = set()
    for s in oz_vault.list_recent_sessions(limit=200):
        seen_agents.add(s["agent"])

    for agent in sorted(seen_agents):
        if agent == "?":
            continue
        results[agent] = reflect_agent(agent, max_sessions=max_sessions)
    return results


def main():
    parser = argparse.ArgumentParser(description="OZ self-improvement reflection")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run")
    run_p.add_argument("--agent", help="single agent (default: all)")
    run_p.add_argument("--sessions", type=int, default=10)

    args = parser.parse_args()
    oz_economy.init_db()
    oz_vault.init_vault()

    if args.cmd == "run":
        if args.agent:
            result = reflect_agent(args.agent, max_sessions=args.sessions)
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            results = reflect_all(max_sessions=args.sessions)
            print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

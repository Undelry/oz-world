"""
oz_vault.py — OZのメモリ・知識ストア (markdown vault)

設計思想:
- 全てのエージェントの記憶・学び・セッション履歴を 1つの markdown vault に書き溜める
- vault の構造は Obsidian と互換 — ユーザーがあとで Obsidian で開ける
- まずは ~/.openclaw/oz_vault/ 配下にあるが、環境変数で Obsidian vault に切替可能
- ベクトル検索ではなく markdown + 全文grep で十分 (RAG軽量版)

ディレクトリ構造:
  oz_vault/
    agents/         # 各エージェントの永続プロファイル
      coder.md
      researcher.md
      hitomi.md
    sessions/       # 1セッション = 1ファイル
      2026-04-09/
        coder_142301_implement-api.md
        researcher_142505_what-is-three-js.md
    knowledge/      # トピック別の累積知識 (reflection が書く)
      project_oz.md
      user_joe_preferences.md
      tools_macos.md
    inbox/          # 自由メモ (ユーザーが書ける)
      idea_001.md

各 markdown ファイルは Obsidian frontmatter 互換:
  ---
  agent: coder
  ts: 2026-04-09T14:23:01
  tags: [implementation, api]
  ---
  本文…

ユーザーが Obsidian を導入したら OZ_VAULT_PATH を設定すれば、そのまま
Obsidian vault として開ける。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# Vault location: env override > Obsidian vault > OZ default
def _resolve_vault_root() -> Path:
    override = os.environ.get("OZ_VAULT_PATH")
    if override:
        return Path(os.path.expanduser(override))
    # Default location — owner-readable
    return Path(os.path.expanduser("~/.openclaw/oz_vault"))


VAULT_ROOT = _resolve_vault_root()
AGENTS_DIR = VAULT_ROOT / "agents"
SESSIONS_DIR = VAULT_ROOT / "sessions"
KNOWLEDGE_DIR = VAULT_ROOT / "knowledge"
INBOX_DIR = VAULT_ROOT / "inbox"

MAX_FRONTMATTER_LINES = 50  # safety against malformed files


def init_vault():
    """Create the directory structure if it doesn't exist."""
    for d in (AGENTS_DIR, SESSIONS_DIR, KNOWLEDGE_DIR, INBOX_DIR):
        d.mkdir(parents=True, exist_ok=True)
    # Owner-only on the root for privacy
    try:
        os.chmod(VAULT_ROOT, 0o700)
    except OSError:
        pass

    # Seed an index README so the user can navigate manually
    readme = VAULT_ROOT / "README.md"
    if not readme.exists():
        readme.write_text(
            "# OZ Vault\n\n"
            "This is your personal OZ memory store. Each agent reads from\n"
            "and writes to this directory.\n\n"
            "- `agents/` — per-agent profile + learned preferences\n"
            "- `sessions/` — one file per agent invocation, sorted by date\n"
            "- `knowledge/` — distilled topic notes from reflection runs\n"
            "- `inbox/` — your own free-form notes that agents can read\n\n"
            "Open this folder in Obsidian to browse with backlinks.\n"
            "Set `OZ_VAULT_PATH` env var to point at an existing Obsidian vault.\n",
            encoding="utf-8",
        )


# ================================
# Frontmatter helpers
# ================================
def _format_frontmatter(meta: dict) -> str:
    if not meta:
        return ""
    lines = ["---"]
    for k, v in meta.items():
        if isinstance(v, list):
            lines.append(f"{k}: [{', '.join(str(x) for x in v)}]")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---\n")
    return "\n".join(lines)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    if len(lines) < 2:
        return {}, text
    # Find closing ---
    end = None
    for i in range(1, min(MAX_FRONTMATTER_LINES, len(lines))):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    meta = {}
    for line in lines[1:end]:
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    body = "\n".join(lines[end + 1:]).lstrip("\n")
    return meta, body


# ================================
# Sessions — every agent invocation gets a note
# ================================
_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9_\-]")


def _safe_slug(text: str, max_len: int = 40) -> str:
    if not text:
        return "untitled"
    slug = _FILENAME_SAFE.sub("-", text)[:max_len].strip("-")
    return slug or "untitled"


def write_session(
    agent: str,
    user_message: str,
    reply: str,
    cost_ozc: float = 0,
    extras: Optional[dict] = None,
) -> str:
    """
    Write a single agent invocation to the session log.

    Returns the path of the written file.
    """
    init_vault()
    now = datetime.now()
    day_dir = SESSIONS_DIR / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)

    slug = _safe_slug(user_message[:30])
    filename = f"{now.strftime('%H%M%S')}_{agent}_{slug}.md"
    path = day_dir / filename

    meta = {
        "agent": agent,
        "ts": now.isoformat(timespec="seconds"),
        "cost_ozc": cost_ozc,
    }
    if extras:
        meta.update(extras)

    body = (
        "## User\n\n"
        f"{user_message}\n\n"
        "## Reply\n\n"
        f"{reply}\n"
    )
    path.write_text(_format_frontmatter(meta) + body, encoding="utf-8")
    return str(path)


def list_recent_sessions(agent: Optional[str] = None, limit: int = 10) -> list[dict]:
    """Return the most recent sessions, optionally filtered by agent."""
    init_vault()
    files = []
    for day_dir in sorted(SESSIONS_DIR.iterdir(), reverse=True):
        if not day_dir.is_dir():
            continue
        for f in sorted(day_dir.iterdir(), reverse=True):
            if not f.name.endswith(".md"):
                continue
            if agent and f"_{agent}_" not in f.name:
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            meta, body = _parse_frontmatter(text)
            files.append({
                "path": str(f),
                "agent": meta.get("agent", "?"),
                "ts": meta.get("ts", ""),
                "cost_ozc": meta.get("cost_ozc", "0"),
                "preview": body[:200].strip(),
            })
            if len(files) >= limit:
                return files
    return files


# ================================
# Agent profile — long-lived per-agent memory
# ================================
def read_agent_profile(agent: str) -> str:
    """Return the agent's persistent profile markdown (or empty string)."""
    init_vault()
    path = AGENTS_DIR / f"{agent}.md"
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def write_agent_profile(agent: str, content: str):
    """Overwrite the agent's persistent profile."""
    init_vault()
    path = AGENTS_DIR / f"{agent}.md"
    path.write_text(content, encoding="utf-8")


def append_to_agent_profile(agent: str, note: str):
    """Append a learned-note to the agent's profile under a Learnings section."""
    init_vault()
    path = AGENTS_DIR / f"{agent}.md"
    existing = read_agent_profile(agent)
    if "## Learnings" not in existing:
        existing = (existing or f"# {agent}\n\n").rstrip() + "\n\n## Learnings\n"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    existing += f"\n- ({timestamp}) {note}\n"
    write_agent_profile(agent, existing)


# ================================
# Knowledge — distilled topics (reflection writes here)
# ================================
def write_knowledge(topic: str, content: str, tags: Optional[list] = None):
    """Write or overwrite a topic note in knowledge/."""
    init_vault()
    slug = _safe_slug(topic, max_len=60)
    path = KNOWLEDGE_DIR / f"{slug}.md"
    meta = {
        "topic": topic,
        "updated": datetime.now().isoformat(timespec="seconds"),
    }
    if tags:
        meta["tags"] = tags
    path.write_text(_format_frontmatter(meta) + content, encoding="utf-8")
    return str(path)


def read_knowledge(topic_slug: str) -> str:
    init_vault()
    path = KNOWLEDGE_DIR / f"{_safe_slug(topic_slug, 60)}.md"
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


# ================================
# Search — lightweight RAG via grep
# ================================
def search(query: str, limit: int = 5) -> list[dict]:
    """
    Full-text search across the entire vault.
    No embedding model — just substring + word match.
    Returns matching files with a snippet of the matching context.
    """
    init_vault()
    if not query.strip():
        return []
    q_lower = query.lower()
    results = []

    for path in VAULT_ROOT.rglob("*.md"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if q_lower not in text.lower():
            continue
        # Find snippet around match
        idx = text.lower().find(q_lower)
        start = max(0, idx - 80)
        end = min(len(text), idx + len(query) + 80)
        snippet = text[start:end].replace("\n", " ").strip()

        results.append({
            "path": str(path.relative_to(VAULT_ROOT)),
            "snippet": snippet,
            "score": text.lower().count(q_lower),  # crude relevance
        })

    results.sort(key=lambda r: -r["score"])
    return results[:limit]


def context_for_agent(agent: str, query: str, max_chars: int = 1500) -> str:
    """
    Build a context snippet to inject into the agent's prompt.
    Combines:
    - the agent's profile
    - top hits from knowledge/
    - recent sessions matching the query
    """
    init_vault()
    parts = []

    profile = read_agent_profile(agent)
    if profile:
        parts.append("## Your previous notes\n\n" + profile.strip())

    if query:
        hits = search(query, limit=3)
        if hits:
            ctx = "## Relevant past notes\n"
            for h in hits:
                ctx += f"\n- {h['path']}: {h['snippet']}"
            parts.append(ctx)

    full = "\n\n".join(parts)
    if len(full) > max_chars:
        full = full[:max_chars] + "\n…(truncated)"
    return full


# ================================
# CLI for inspection
# ================================
def main():
    import argparse

    parser = argparse.ArgumentParser(description="OZ Vault CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Create vault directories")
    sub.add_parser("path", help="Print vault root path")

    sessions_p = sub.add_parser("sessions", help="List recent sessions")
    sessions_p.add_argument("--agent")
    sessions_p.add_argument("--limit", type=int, default=10)

    profile_p = sub.add_parser("profile")
    profile_p.add_argument("agent")

    search_p = sub.add_parser("search")
    search_p.add_argument("query")

    context_p = sub.add_parser("context", help="Build the context an agent would see")
    context_p.add_argument("agent")
    context_p.add_argument("query")

    args = parser.parse_args()

    if args.cmd == "init":
        init_vault()
        print(f"Initialized: {VAULT_ROOT}")
    elif args.cmd == "path":
        print(VAULT_ROOT)
    elif args.cmd == "sessions":
        for s in list_recent_sessions(args.agent, args.limit):
            print(f"  [{s['ts']}] {s['agent']:12} {s['cost_ozc']} OZC")
            print(f"    {s['preview'][:100]}")
            print(f"    {s['path']}")
    elif args.cmd == "profile":
        print(read_agent_profile(args.agent) or "(no profile)")
    elif args.cmd == "search":
        for r in search(args.query):
            print(f"  [{r['score']}] {r['path']}")
            print(f"    {r['snippet']}")
    elif args.cmd == "context":
        print(context_for_agent(args.agent, args.query))


if __name__ == "__main__":
    main()

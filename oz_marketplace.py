"""
oz_marketplace.py — OZのスキルマーケットプレイス

スキルとは:
- markdown ファイル一つで定義された「能力」
- ~/.claude/skills/ に置けば Claude Code エージェントが自動的に使える
- OZ では「公開・評価・購入・OZC報酬」のレイヤを追加する

データ構造:
  ~/.openclaw/oz_marketplace/
    skills.db          -- SQLite (catalog + ratings)
    skills/            -- 公開されている skill markdown
      <skill_id>.md
    installed/         -- インストール済み (~/.claude/skills/oz/ への symlink)

機能:
- publish(name, description, body, author, price_ozc=0) -- スキル登録
- list(tag=None, sort='popular') -- スキル一覧
- get(skill_id) -- 1つのスキルの詳細
- install(skill_id) -- ~/.claude/skills/oz/ にコピー
- rate(skill_id, rater, stars, comment) -- 評価する
- 評価時に自動で著者に OZC を支払う (4★=1, 5★=3 OZC)

制約:
- 自分のスキル評価は無効 (rater == author を弾く)
- rate-limit: 同じ rater から同じ skill への評価は1日1回まで
- 評価の改竄: ratings は ledger と同じく追記のみ、削除不可

中央集権を避ける:
- 全ては local SQLite + local markdown
- 将来 P2P 同期 (rsync, Tailscale, libp2p) で他のOZと共有
- そのときも各人の OZ が「自分のキュレーション」を持つ
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import oz_economy

ROOT = Path(os.path.expanduser("~/.openclaw/oz_marketplace"))
DB_PATH = ROOT / "skills.db"
SKILLS_DIR = ROOT / "skills"
INSTALLED_DIR = Path(os.path.expanduser("~/.claude/skills/oz"))

# OZC reward table for ratings
RATING_REWARDS = {
    1: 0,
    2: 0,
    3: 0,
    4: 1,    # 良い
    5: 3,    # 素晴らしい
}

# How much it costs to publish (prevents spam)
PUBLISH_FEE_OZC = 5


# ================================
# DB setup
# ================================
def _init():
    ROOT.mkdir(parents=True, exist_ok=True)
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(ROOT, 0o700)
    except OSError:
        pass

    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS skills (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            author TEXT NOT NULL,
            tags TEXT,
            price_ozc REAL DEFAULT 0,
            published_at REAL NOT NULL,
            install_count INTEGER DEFAULT 0,
            avg_rating REAL DEFAULT 0,
            rating_count INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ratings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_id TEXT NOT NULL,
            rater TEXT NOT NULL,
            stars INTEGER NOT NULL,
            comment TEXT,
            ts REAL NOT NULL,
            UNIQUE(skill_id, rater, ts)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ratings_skill ON ratings(skill_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_skills_pop ON skills(install_count DESC, avg_rating DESC)")
    conn.close()
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass


def _conn():
    return sqlite3.connect(DB_PATH, isolation_level=None)


# ================================
# Publish
# ================================
def _make_skill_id(name: str, author: str) -> str:
    h = hashlib.sha256(f"{name}|{author}|{time.time()}".encode("utf-8")).hexdigest()
    return h[:16]


def publish(name: str, description: str, body: str, author: str,
            tags: Optional[list] = None, price_ozc: float = 0) -> dict:
    """
    Publish a new skill. The author pays a small OZC fee to prevent spam.
    """
    _init()
    if not name or not body:
        return {"ok": False, "error": "name and body required"}
    if len(name) > 80 or len(body) > 50000:
        return {"ok": False, "error": "name or body too long"}
    if price_ozc < 0:
        return {"ok": False, "error": "price must be non-negative"}

    # Charge the author the publish fee
    try:
        oz_economy.transfer(author, "treasury", PUBLISH_FEE_OZC, "marketplace.publish", name[:60])
    except ValueError as e:
        return {"ok": False, "error": f"cannot charge publish fee: {e}"}

    skill_id = _make_skill_id(name, author)

    # Write the markdown file with frontmatter
    md = (
        "---\n"
        f"id: {skill_id}\n"
        f"name: {name}\n"
        f"author: {author}\n"
        f"price_ozc: {price_ozc}\n"
        f"tags: {', '.join(tags or [])}\n"
        f"published_at: {datetime.now().isoformat(timespec='seconds')}\n"
        "---\n\n"
        f"# {name}\n\n"
        f"> {description}\n\n"
        f"{body}\n"
    )
    skill_path = SKILLS_DIR / f"{skill_id}.md"
    skill_path.write_text(md, encoding="utf-8")

    # Insert into catalog
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO skills (id, name, description, author, tags, price_ozc, published_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (skill_id, name[:80], description[:300], author[:64],
          ",".join(tags or []), price_ozc, time.time()))
    conn.close()

    return {
        "ok": True,
        "skill_id": skill_id,
        "name": name,
        "author": author,
        "publish_fee_charged": PUBLISH_FEE_OZC,
        "path": str(skill_path),
    }


# ================================
# Browse
# ================================
def list_skills(tag: Optional[str] = None, sort: str = "popular", limit: int = 50) -> list[dict]:
    _init()
    conn = _conn()
    cur = conn.cursor()
    if sort == "newest":
        order = "published_at DESC"
    elif sort == "rating":
        order = "avg_rating DESC, rating_count DESC"
    else:  # popular
        order = "install_count DESC, avg_rating DESC"

    if tag:
        cur.execute(f"""
            SELECT id, name, description, author, tags, price_ozc, install_count, avg_rating, rating_count, published_at
            FROM skills WHERE tags LIKE ?
            ORDER BY {order} LIMIT ?
        """, (f"%{tag}%", limit))
    else:
        cur.execute(f"""
            SELECT id, name, description, author, tags, price_ozc, install_count, avg_rating, rating_count, published_at
            FROM skills ORDER BY {order} LIMIT ?
        """, (limit,))
    rows = cur.fetchall()
    conn.close()

    return [
        {
            "id": r[0], "name": r[1], "description": r[2], "author": r[3],
            "tags": r[4].split(",") if r[4] else [],
            "price_ozc": r[5],
            "install_count": r[6],
            "avg_rating": round(r[7] or 0, 2),
            "rating_count": r[8],
            "published_at": datetime.fromtimestamp(r[9]).isoformat(timespec="seconds"),
        }
        for r in rows
    ]


def get_skill(skill_id: str) -> Optional[dict]:
    _init()
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name, description, author, tags, price_ozc, install_count, avg_rating, rating_count, published_at FROM skills WHERE id = ?", (skill_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return None

    cur.execute("SELECT rater, stars, comment, ts FROM ratings WHERE skill_id = ? ORDER BY ts DESC LIMIT 20", (skill_id,))
    ratings = [
        {
            "rater": r[0], "stars": r[1], "comment": r[2],
            "ts": datetime.fromtimestamp(r[3]).isoformat(timespec="seconds"),
        }
        for r in cur.fetchall()
    ]
    conn.close()

    skill_path = SKILLS_DIR / f"{skill_id}.md"
    body = skill_path.read_text(encoding="utf-8") if skill_path.exists() else ""

    return {
        "id": row[0], "name": row[1], "description": row[2], "author": row[3],
        "tags": row[4].split(",") if row[4] else [],
        "price_ozc": row[5],
        "install_count": row[6],
        "avg_rating": round(row[7] or 0, 2),
        "rating_count": row[8],
        "published_at": datetime.fromtimestamp(row[9]).isoformat(timespec="seconds"),
        "body": body,
        "recent_ratings": ratings,
    }


# ================================
# Install
# ================================
def install_skill(skill_id: str) -> dict:
    """
    Copy the skill markdown to ~/.claude/skills/oz/ so Claude Code can use it.
    Charges price_ozc if any.
    """
    _init()
    skill = get_skill(skill_id)
    if skill is None:
        return {"ok": False, "error": "unknown skill"}

    src = SKILLS_DIR / f"{skill_id}.md"
    if not src.exists():
        return {"ok": False, "error": "skill body missing"}

    INSTALLED_DIR.mkdir(parents=True, exist_ok=True)
    dst = INSTALLED_DIR / f"{skill['name'].replace('/', '-').replace(' ', '-')}.md"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    # Charge the price (if any), pay the author
    if skill["price_ozc"] > 0:
        try:
            oz_economy.transfer("hitomi", skill["author"], skill["price_ozc"],
                                "marketplace.install", skill["name"][:60])
        except Exception:
            pass

    # Bump install count
    conn = _conn()
    cur = conn.cursor()
    cur.execute("UPDATE skills SET install_count = install_count + 1 WHERE id = ?", (skill_id,))
    conn.close()

    return {"ok": True, "skill_id": skill_id, "installed_at": str(dst)}


# ================================
# Rate (and reward author)
# ================================
def rate_skill(skill_id: str, rater: str, stars: int, comment: str = "") -> dict:
    """
    Rate a skill. Triggers OZC reward to the author.
    Same rater + same skill in same calendar day = rejected.
    """
    _init()
    if stars not in (1, 2, 3, 4, 5):
        return {"ok": False, "error": "stars must be 1-5"}

    skill = get_skill(skill_id)
    if skill is None:
        return {"ok": False, "error": "unknown skill"}

    if rater == skill["author"]:
        return {"ok": False, "error": "cannot rate your own skill"}

    # Rate-limit: same rater + same skill within 24h
    conn = _conn()
    cur = conn.cursor()
    day_start = time.time() - 86400
    cur.execute("""
        SELECT COUNT(*) FROM ratings
        WHERE skill_id = ? AND rater = ? AND ts > ?
    """, (skill_id, rater, day_start))
    if cur.fetchone()[0] > 0:
        conn.close()
        return {"ok": False, "error": "already rated within 24h"}

    # Insert rating
    now_ts = time.time()
    cur.execute("""
        INSERT INTO ratings (skill_id, rater, stars, comment, ts)
        VALUES (?, ?, ?, ?, ?)
    """, (skill_id, rater[:64], stars, comment[:500], now_ts))

    # Recompute aggregate
    cur.execute("SELECT AVG(stars), COUNT(*) FROM ratings WHERE skill_id = ?", (skill_id,))
    avg, count = cur.fetchone()
    cur.execute("UPDATE skills SET avg_rating = ?, rating_count = ? WHERE id = ?",
                (avg, count, skill_id))
    conn.close()

    # Reward the author from treasury
    reward = RATING_REWARDS.get(stars, 0)
    tx = None
    if reward > 0:
        try:
            tx = oz_economy.transfer(
                "treasury", skill["author"], reward,
                "marketplace.reward", f"{skill_id}:{stars}★",
            )
        except Exception:
            pass

    return {
        "ok": True,
        "skill_id": skill_id,
        "stars": stars,
        "new_avg": round(avg or 0, 2),
        "new_count": count,
        "reward_ozc": reward,
        "reward_tx_id": tx["id"] if tx else None,
    }


# ================================
# Seed sample skills (for the empty marketplace at first run)
# ================================
def seed_sample_skills():
    """Add a few starter skills if the marketplace is empty."""
    _init()
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM skills")
    if cur.fetchone()[0] > 0:
        conn.close()
        return {"seeded": False, "reason": "already populated"}
    conn.close()

    samples = [
        {
            "name": "macOSアプリ起動",
            "description": "Macのアプリ名を言うだけで起動するスキル",
            "author": "hitomi",
            "tags": ["macos", "automation", "starter"],
            "price_ozc": 0,
            "body": (
                "## 使い方\n\n"
                "アプリ名を言うと、osascript を使って起動します。\n\n"
                "```bash\n"
                "osascript -e 'tell application \"<App名>\" to activate'\n"
                "```\n\n"
                "## 注意\n\n"
                "- 不明なアプリは絶対に起動しない\n"
                "- ユーザーの承認を得てから実行する\n"
            ),
        },
        {
            "name": "Spotlight検索",
            "description": "ファイル名から場所を見つけるスキル",
            "author": "researcher",
            "tags": ["macos", "search", "starter"],
            "price_ozc": 0,
            "body": (
                "## 使い方\n\n"
                "ファイル名やキーワードから Spotlight 検索をする。\n\n"
                "```bash\n"
                "mdfind -name 'PDF'\n"
                "mdfind 'kMDItemContentType == public.pdf'\n"
                "```\n"
            ),
        },
        {
            "name": "ジャーナルを書く",
            "description": "今日の出来事を Obsidian vault に追記",
            "author": "writer",
            "tags": ["journal", "obsidian", "starter"],
            "price_ozc": 0,
            "body": (
                "## 使い方\n\n"
                "今日のジャーナルファイルに追記する。\n\n"
                "```bash\n"
                "DATE=$(date +%Y-%m-%d)\n"
                "echo '...' >> ~/.openclaw/oz_vault/knowledge/journal_$DATE.md\n"
                "```\n"
            ),
        },
    ]

    out = []
    for s in samples:
        # Bypass the publish fee for sample skills
        skill_id = _make_skill_id(s["name"], s["author"])
        skill_path = SKILLS_DIR / f"{skill_id}.md"
        md = (
            "---\n"
            f"id: {skill_id}\n"
            f"name: {s['name']}\n"
            f"author: {s['author']}\n"
            f"price_ozc: 0\n"
            f"tags: {', '.join(s['tags'])}\n"
            f"published_at: {datetime.now().isoformat(timespec='seconds')}\n"
            "---\n\n"
            f"# {s['name']}\n\n"
            f"> {s['description']}\n\n"
            f"{s['body']}\n"
        )
        skill_path.write_text(md, encoding="utf-8")

        conn = _conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO skills (id, name, description, author, tags, price_ozc, published_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (skill_id, s["name"], s["description"], s["author"],
              ",".join(s["tags"]), 0, time.time()))
        conn.close()
        out.append(skill_id)

    return {"seeded": True, "skill_ids": out}


# ================================
# CLI
# ================================
def main():
    import argparse
    parser = argparse.ArgumentParser(description="OZ Marketplace CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    sub.add_parser("seed")

    list_p = sub.add_parser("list")
    list_p.add_argument("--tag")
    list_p.add_argument("--sort", default="popular", choices=["popular", "newest", "rating"])

    get_p = sub.add_parser("get")
    get_p.add_argument("skill_id")

    publish_p = sub.add_parser("publish")
    publish_p.add_argument("--name", required=True)
    publish_p.add_argument("--description", default="")
    publish_p.add_argument("--author", required=True)
    publish_p.add_argument("--body", required=True)
    publish_p.add_argument("--tags", default="")

    rate_p = sub.add_parser("rate")
    rate_p.add_argument("skill_id")
    rate_p.add_argument("stars", type=int)
    rate_p.add_argument("--rater", default="human")
    rate_p.add_argument("--comment", default="")

    install_p = sub.add_parser("install")
    install_p.add_argument("skill_id")

    args = parser.parse_args()
    oz_economy.init_db()

    if args.cmd == "init":
        _init()
        print(f"initialized at {ROOT}")
    elif args.cmd == "seed":
        print(json.dumps(seed_sample_skills(), indent=2))
    elif args.cmd == "list":
        for s in list_skills(tag=args.tag, sort=args.sort):
            stars = "★" * int(s["avg_rating"])
            print(f"  {s['id']}  {s['name'][:30]:32} by {s['author']:14} {stars} ({s['rating_count']}) installs={s['install_count']}")
    elif args.cmd == "get":
        s = get_skill(args.skill_id)
        if s is None:
            print("not found")
        else:
            print(json.dumps(s, indent=2, ensure_ascii=False))
    elif args.cmd == "publish":
        result = publish(
            args.name, args.description, args.body, args.author,
            tags=args.tags.split(",") if args.tags else None,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.cmd == "rate":
        result = rate_skill(args.skill_id, args.rater, args.stars, args.comment)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif args.cmd == "install":
        result = install_skill(args.skill_id)
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

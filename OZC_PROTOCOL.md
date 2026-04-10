# OZC Protocol 1.0

A settlement protocol for AI agent transactions.

**Status**: Draft — 2026-04-11
**Reference implementation**: `oz_economy.py`, `oz_identity.py`, `oz_onchain.py`
**Daemon**: `python3 oz_economy.py serve --port 8800`

---

## 1. Overview

OZC (OZ Coin) is a settlement protocol that lets AI agents pay each other,
record what they did, and prove it later. It is **not** a chat framework, an
agent runtime, or a UI. It is the layer underneath: a tamper-evident ledger
exposed over plain HTTP/JSON, with cryptographic identity and an optional
on-chain bridge to a real Solana token. Any agent — Claude Code, OpenClaw,
LangChain, AutoGPT, a one-line shell script — can register, send OZC, and
read history with three `curl` calls. The reference implementation is one
Python file, no framework dependency, no daemon framework, stdlib only.

## 2. Design Philosophy

Protocols outlive frameworks. Email is older than every email client; HTTP
is older than every browser. OZC follows the same rule: define the wire
format and the data model, then let any client speak it.

**Why HTTP/JSON instead of gRPC, GraphQL, or a Python SDK**:
- Every language has an HTTP client in its standard library
- Every developer can `curl` a JSON endpoint without reading docs
- Proxies, firewalls, browsers, and `tcpdump` all understand it
- A SDK locks callers to one language; a protocol does not

**Why stdlib-only server**:
- `pip install ozc` should not pull FastAPI, Flask, uvicorn, pydantic
- A protocol with 8 dependencies is not a protocol, it is a product
- `http.server.ThreadingHTTPServer` has shipped with Python since 1.x and
  handles thousands of QPS for the workload OZC needs

**Framework-independence is mandatory**:
- The same daemon serves OpenClaw, Claude Code, LangChain, an iPhone app,
  a Three.js viewer, a Bash cron, and another OZC instance over a relay
- Identity (Ed25519) is portable: a key generated in Rust verifies in JS
- The ledger format is deterministic JSON over SHA-256, reproducible in
  any language

**Single source of truth**:
- The SQLite ledger at `~/.openclaw/workspace/oz_economy.db` is canonical
- Every other surface (CLI, HTTP daemon, on-chain bridge, future relay
  gossip) reads and writes through the same locked transaction path

---

## 3. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Display Layer (any UI)                  │
│  ┌────────────┐  ┌────────────┐  ┌──────────┐  ┌────────┐  │
│  │ OZ 3D World│  │  CLI tool  │  │ Mobile   │  │ Slack  │  │
│  │ (Three.js) │  │  (curl/sh) │  │ app      │  │ bot    │  │
│  └─────┬──────┘  └──────┬─────┘  └────┬─────┘  └───┬────┘  │
└────────┼────────────────┼──────────────┼────────────┼──────┘
         │                │              │            │
         └────────────────┴──────────────┴────────────┘
                          │
                  HTTP / JSON / X-OZ-Token
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                   OZC Protocol Layer                        │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │            OZC HTTP Daemon (port 8800)              │    │
│  │  /status /balance /balances /ledger /reputation     │    │
│  │  /register /transfer                                │    │
│  └─────────────────────┬───────────────────────────────┘    │
│                        │                                    │
│  ┌─────────────────────▼───────────────────────────────┐    │
│  │              Core Functions (Python)                │    │
│  │  transfer() · register_agent() · get_reputation()   │    │
│  │  verify_chain() · get_balance() · get_ledger()      │    │
│  └─────────────────────┬───────────────────────────────┘    │
│                        │                                    │
│   ┌────────────────────┼─────────────────────┐              │
│   │                    │                     │              │
│   ▼                    ▼                     ▼              │
│ ┌──────────┐    ┌──────────────┐    ┌──────────────────┐    │
│ │ SQLite   │    │  Ed25519     │    │  Solana RPC      │    │
│ │ ledger   │    │  identity    │    │  (read-only,     │    │
│ │ + chain  │    │  (signing)   │    │   optional)      │    │
│ └──────────┘    └──────────────┘    └──────────────────┘    │
│  oz_economy.py   oz_identity.py     oz_onchain.py           │
└─────────────────────────────────────────────────────────────┘
```

The display layer never reaches into SQLite or Solana directly. The
protocol layer never assumes anything about the display layer. Either side
can be replaced without touching the other.

---

## 4. API Reference

All endpoints live under the daemon root (default `http://127.0.0.1:8800`).
All requests must include `X-OZ-Token: <token>` where `<token>` is the
content of `~/.openclaw/oz_token`. The daemon refuses to start without a
token unless invoked with `--no-auth` (development only).

Responses are JSON. Successful responses have `"ok": true`. Errors have
`"ok": false` and a string `"error"` field. HTTP status codes follow
convention: 200 success, 400 bad request, 401 unauthorized, 404 unknown
route, 413 payload too large, 500 internal error.

---

### `GET /status`

Daemon health and totals.

**Request**: none.

**Response**:
```json
{
  "ok": true,
  "service": "ozc-daemon",
  "version": "1.0",
  "uptime_seconds": 312.4,
  "db_path": "/Users/.../oz_economy.db",
  "agents_registered": 12,
  "ledger_blocks": 1098,
  "daily_cap_ozc": 5000,
  "ozc_to_jpy": 1.0,
  "onchain_bridge": true
}
```

**Example**:
```bash
curl -H "X-OZ-Token: $TOKEN" http://127.0.0.1:8800/status
```

---

### `GET /balance/<agent>`

Current balance for a single agent.

**Request**: `<agent>` is a path segment, max 64 characters, alphanumeric +
underscore + hyphen.

**Response**:
```json
{ "ok": true, "agent": "coder", "balance": 1838.0 }
```

If the agent does not exist the response still returns `"balance": 0.0` —
unknown and zero are indistinguishable by design (use `/register` to assert
existence).

**Example**:
```bash
curl -H "X-OZ-Token: $TOKEN" http://127.0.0.1:8800/balance/coder
```

---

### `GET /balances`

All known balances at once. Useful for dashboards.

**Response**:
```json
{
  "ok": true,
  "balances": {
    "treasury": 998641.0,
    "human":    99999.0,
    "hitomi":   1005.0,
    "coder":    1838.0
  }
}
```

---

### `GET /ledger?limit=N&offset=N&since=TS`

Recent transactions. Cursor-style with `limit` + `offset`. `since` is a
Unix timestamp filter (only transactions newer than `since`).

**Query parameters**:
| name | type | default | max |
|---|---|---|---|
| `limit` | int | 50 | 1000 |
| `offset` | int | 0 | — |
| `since` | float (unix ts) | none | — |

**Response**:
```json
{
  "ok": true,
  "limit": 50,
  "offset": 0,
  "count": 50,
  "transactions": [
    {
      "id": 1100,
      "ts": 1775835420.207978,
      "from_agent": "hitomi",
      "to_agent": "newagent43",
      "amount": 3.0,
      "action": "manual",
      "detail": "daemon test 2",
      "from_balance_after": 1002.0,
      "to_balance_after": 53.0,
      "prev_hash": "ab00bdc1...",
      "hash": "4dfbf31e...",
      "signer_pubkey": "fa154b8b...",
      "sig": "5745b31d..."
    }
  ]
}
```

`signer_pubkey` and `sig` are present on transactions made after Phase 2.
Pre-Phase-2 transactions verify by hash chain alone.

**Example**:
```bash
curl -H "X-OZ-Token: $TOKEN" "http://127.0.0.1:8800/ledger?limit=10"
curl -H "X-OZ-Token: $TOKEN" "http://127.0.0.1:8800/ledger?since=1775835000"
```

---

### `GET /reputation/<agent>`

Aggregate metrics computed from the ledger.

**Response**:
```json
{
  "ok": true,
  "agent": "coder",
  "balance": 1838.0,
  "tasks_assigned": 74,
  "tasks_completed": 91,
  "completion_rate": 1.23,
  "total_earned": 3068.0,
  "total_spent": 1730.0,
  "net": 1338.0,
  "actions_charged": 2,
  "first_seen": 1775664562.55,
  "last_seen": 1775712559.91,
  "tx_count": 168
}
```

**Field semantics**:
- `tasks_assigned`: count of `task.assign` transactions where `to == agent`
- `tasks_completed`: count of `task.report` transactions where `from == agent`
- `completion_rate`: `tasks_completed / tasks_assigned` (`null` if no tasks
  assigned). May exceed 1.0 if the agent reported tasks that were assigned
  before the current ledger window
- `total_earned`: sum of all incoming transfer amounts
- `total_spent`: sum of all outgoing transfer amounts
- `net`: `total_earned - total_spent`
- `actions_charged`: count of `llm.*` actions charged to the agent
- `first_seen` / `last_seen`: Unix timestamps of the first and last ledger
  rows touching this agent
- `tx_count`: total number of ledger rows touching this agent

**Example**:
```bash
curl -H "X-OZ-Token: $TOKEN" http://127.0.0.1:8800/reputation/coder
```

---

### `POST /register`

Register a new agent. Idempotent: re-registering an existing agent returns
the existing balance instead of overwriting.

**Request body**:
```json
{
  "agent": "my_new_bot",
  "initial_balance": 100
}
```

| field | type | required | notes |
|---|---|---|---|
| `agent` | string | yes | max 64 chars, will be `.strip()`-ed |
| `initial_balance` | number | no | default 0; if > 0, minted via a `register` transfer from `treasury` |

**Response (new agent)**:
```json
{
  "ok": true,
  "agent": "my_new_bot",
  "balance": 100.0,
  "created": true
}
```

**Response (existing agent)**:
```json
{
  "ok": true,
  "agent": "my_new_bot",
  "balance": 100.0,
  "created": false
}
```

If treasury cannot fund the requested initial balance, the agent is still
registered with balance 0 and the response includes a `warning` field.

**Example**:
```bash
curl -X POST -H "X-OZ-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"agent":"my_new_bot","initial_balance":100}' \
  http://127.0.0.1:8800/register
```

---

### `POST /transfer`

Move OZC from one agent to another. The transaction is signed by the
daemon's local Ed25519 identity (`~/.openclaw/oz_identity.ed25519`) and
appended to the chain.

**Request body**:
```json
{
  "from": "hitomi",
  "to": "coder",
  "amount": 10,
  "action": "agent_payment",
  "detail": "PR #42 review"
}
```

| field | type | required | notes |
|---|---|---|---|
| `from` | string | yes | also accepts `from_agent` |
| `to` | string | yes | also accepts `to_agent` |
| `amount` | number | yes | non-negative; must not exceed `from`'s balance |
| `action` | string | no | default `"manual"`, max 40 chars; see §7 for canonical types |
| `detail` | string | no | free-form description, max 200 chars |

**Response**:
```json
{
  "ok": true,
  "transaction": {
    "id": 1100,
    "ts": 1775835420.207978,
    "from_agent": "hitomi",
    "to_agent": "coder",
    "amount": 10.0,
    "action": "agent_payment",
    "detail": "PR #42 review",
    "from_balance_after": 995.0,
    "to_balance_after": 1848.0,
    "prev_hash": "ab00bdc1...",
    "hash": "4dfbf31e...",
    "signer_pubkey": "fa154b8b...",
    "sig": "5745b31d..."
  }
}
```

**Errors**:
- `400 insufficient balance: <agent> has X, need Y` — sender cannot afford it
- `400 daily budget cap exceeded` — `DAILY_BUDGET_CAP` breached
- `400 monthly real-money cap exceeded` — `MONTHLY_REAL_CAP_JPY` breached
- `400 from_agent and to_agent must differ`
- `400 amount must be non-negative`

**Example**:
```bash
curl -X POST -H "X-OZ-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"from":"hitomi","to":"coder","amount":10,"action":"agent_payment","detail":"PR #42"}' \
  http://127.0.0.1:8800/transfer
```

---

## 5. Identity

OZC uses **Ed25519** for cryptographic identity. Reference: `oz_identity.py`.

**Why Ed25519**:
- Small keys (32 bytes) and signatures (64 bytes)
- Deterministic signatures — no nonce reuse attacks
- Implemented in every language's standard crypto library
- Used by SSH, Nostr, Bluesky, modern TLS, age, signify

**Key files**:
| path | mode | format |
|---|---|---|
| `~/.openclaw/oz_identity.ed25519` | 0600 | raw 32-byte private key seed |
| `~/.openclaw/oz_identity.pub` | 0644 | hex-encoded 32-byte public key + newline |

**Wire format**:
- Public key: 64-character lowercase hex
- Signature: 128-character lowercase hex
- Signed events: JSON object with `pubkey` and `sig` fields appended

**Canonical signing payload**: signatures are computed over the canonical
JSON of an object with sorted keys, no whitespace, UTF-8 encoded:
```python
json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
```

**Generating an identity**:
```bash
python3 oz_identity.py init           # creates the keypair
python3 oz_identity.py show           # prints the public key
```

**Signing arbitrary data**:
```bash
python3 oz_identity.py sign "hello"
# → hex-encoded 64-byte signature

python3 oz_identity.py verify "hello" <sig> <pubkey>
# → "valid" or "INVALID" + nonzero exit
```

**Signed events** (high-level API):
```python
import oz_identity
event = {"type": "ozc.transfer", "from": "joe", "to": "coder", "amount": 10}
signed = oz_identity.sign_event(event)
# {"type": "ozc.transfer", ..., "pubkey": "fa15...", "sig": "06ed..."}
oz_identity.verify_event(signed)  # → True
```

---

## 6. Ledger

The OZC ledger is a SHA-256 hash chain stored in SQLite.

**Database**: `~/.openclaw/workspace/oz_economy.db`
**Mode**: WAL, 0600 permissions, owner-only.

**Schema** (table `ledger`):
```sql
CREATE TABLE ledger (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                  REAL    NOT NULL,
  from_agent          TEXT    NOT NULL,
  to_agent            TEXT    NOT NULL,
  amount              REAL    NOT NULL,
  action              TEXT    NOT NULL,
  detail              TEXT,
  from_balance_after  REAL,
  to_balance_after    REAL,
  prev_hash           TEXT,
  hash                TEXT,
  signer_pubkey       TEXT,    -- nullable for pre-Phase-2 rows
  sig                 TEXT     -- nullable for pre-Phase-2 rows
);
```

**Block hashing**: each block's `hash` is `SHA-256(canonical_json(payload))`
where the payload contains the previous block's hash, the current row's
identifying fields, and (since Phase 2) the signer pubkey and signature.

```python
def _compute_block_hash(tx):
    base = {
        "id":     int(tx["id"]),
        "ts":     round(float(tx["ts"]), 6),
        "from":   tx["from_agent"],
        "to":     tx["to_agent"],
        "amount": round(float(tx["amount"]), 6),
        "action": tx["action"],
        "detail": tx["detail"] or "",
        "prev":   tx["prev_hash"],
    }
    if tx.get("signer_pubkey"):  # Phase 2+
        base["signer"] = tx["signer_pubkey"]
        base["sig"]    = tx.get("sig") or ""
    payload = json.dumps(base, sort_keys=True, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()
```

**Genesis hash**: `"0" * 64` (sixty-four zeros).

**Backwards compatibility**: pre-Phase-2 rows omit `signer` and `sig` from
the hash payload entirely. This lets a chain that has both unsigned and
signed blocks verify cleanly.

**Verification** (`verify_chain()`): walks the entire ledger from id=1.
For each row:
1. Recompute the block hash and compare to stored `hash`
2. Compare `prev_hash` to the previous block's `hash`
3. If `signer_pubkey` is present, recompute the pre-sig hash and verify
   the Ed25519 signature against `signer_pubkey`

Verification result:
```json
{ "ok": true, "total": 1098, "valid": 1098, "signed": 1, "broken_at": null }
```

If any block fails, `ok` is `false`, `broken_at` is the offending block id,
and `reason` is one of `"prev_hash mismatch"`, `"hash mismatch"`,
`"signature invalid"`.

**Atomic write rule**: before appending a new block, the most recent block
is re-hashed and compared to its stored hash. If the chain is already
broken, the daemon refuses to extend it. This blocks "edit then append"
attacks.

---

## 7. Transaction Types

The `action` field on a transaction is free-form text (max 40 chars), but
the protocol defines canonical types so different implementations agree on
semantics. Implementations should prefer these names.

| action | direction | meaning |
|---|---|---|
| `agent_payment` | A → B | A pays B for performing work (the canonical case) |
| `tip` | A → B | Voluntary, unsolicited payment (no obligation discharged) |
| `api_purchase` | A → B | A pays B for access to a resource or API call |
| `prediction` | A → B | A bets OZC on B's claim/forecast (settled later) |
| `exchange` | A → B | Atomic swap or trade settlement |
| `task.assign` | A → B | A assigns a task to B with budget attached |
| `task.report` | A → B | B returns leftover budget when reporting completion |
| `topup` | treasury → A | Mint new OZC into A from the treasury |
| `register` | treasury → A | Initial balance grant on registration |
| `manual` | A → B | Default for unclassified human-initiated transfers |

**Reserved action names**: actions matching `llm.*`, `tts.*`, `stt.*`,
`file.*`, `http.*` are reserved for `charge_action()` calls and indicate
the agent paid the system (treasury) for a resource.

**Cap exemptions**: actions in `{topup, auto.topup, auction.win,
task.assign, task.report, register}` are excluded from daily/monthly
budget caps because they are accounting moves rather than spending.

---

## 8. Reputation

Reputation is computed from the ledger only. There is no separate
reputation table — the ledger is the truth, reputation is a view over it.

**Computation** (see `get_reputation()` in `oz_economy.py`):

```
tasks_assigned   = COUNT(action = 'task.assign'  AND to_agent   = X)
tasks_completed  = COUNT(action = 'task.report'  AND from_agent = X)
total_earned     = SUM(amount WHERE to_agent     = X)
total_spent      = SUM(amount WHERE from_agent   = X)
actions_charged  = COUNT(action LIKE 'llm.%'     AND from_agent = X)
first_seen       = MIN(ts WHERE from_agent = X OR to_agent = X)
last_seen        = MAX(ts WHERE from_agent = X OR to_agent = X)
tx_count         = COUNT(*  WHERE from_agent = X OR to_agent = X)
completion_rate  = tasks_completed / tasks_assigned   (or null)
net              = total_earned - total_spent
```

**Properties**:
- **Verifiable**: anyone with a copy of the ledger computes the same numbers
- **Non-falsifiable**: changing reputation requires changing ledger rows,
  which breaks the hash chain
- **Time-windowed**: clients may filter by `since=` to get reputation for
  a recent window only
- **Composite-friendly**: clients are free to combine the raw fields into
  custom scores (weighted completion rate, recency-decayed earnings, etc)

**Anti-gaming**:
- Self-transfers (`from == to`) are rejected at the transfer layer
- Daily and monthly caps make spam transfers expensive
- Future: signed events with relay gossip make reputation portable across
  OZC instances; a Sybil-resistant scheme is not part of v1.0

---

## 9. On-chain Bridge

OZC has an optional bridge to the real OZC SPL token on Solana. Reference:
`oz_onchain.py`. This module is **read-only by design**: it observes the
on-chain world without ever signing or sending.

**Token**:
- Mint: `AHZWRiVYmSw1Dr7y52GeJPwvo6Gwsbe5Y4t9fPWiis6F`
- Network: Solana mainnet-beta
- RPC: `https://api.mainnet-beta.solana.com` (override with `OZ_SOLANA_RPC`)
- Program: SPL Token Program

**What the bridge does**:
| function | purpose |
|---|---|
| `get_ozc_total_supply()` | Total OZC supply on-chain |
| `get_ozc_balance(wallet)` | OZC balance of any Solana wallet |
| `get_sol_balance(wallet)` | Native SOL balance of any wallet |
| `load_wallets()` | Read agent → wallet mapping from `oz_wallets.json` |
| `get_agent_onchain_balance(agent)` | Convenience: agent name → on-chain OZC |

**What the bridge does not do** (deliberately):
- No private keys
- No signing
- No sending tokens
- No mint authority operations
- No Raydium / DEX interactions

**Why read-only first**: minting and sending require key custody, which
requires a security model we have not yet specified. The read-only layer
is useful immediately (dashboards, balance display, audit) and can be
extended later without changing this spec.

**Wallet directory** (`~/.openclaw/workspace/oz_wallets.json`):
```json
{
  "version": 1,
  "wallets": {
    "hitomi":   { "address": "...", "role": "founder",  "notes": "..." },
    "treasury": { "address": "...", "role": "treasury", "notes": "..." }
  }
}
```

**Caching**: the bridge has a 30-second in-memory TTL cache to avoid
rate-limiting on the public RPC.

**Reversibility**: deleting `oz_onchain.py` does not break anything else.
`oz_economy.py` imports it lazily and falls back cleanly. The bridge is
truly optional.

**CLI**:
```bash
python3 oz_onchain.py supply
python3 oz_onchain.py balance <wallet_address>
python3 oz_onchain.py agents
```

**Reflected in `/status`**: the daemon's `/status` response includes
`"onchain_bridge": true|false` so clients can detect availability.

---

## 10. Quick Start

Three commands to verify the daemon is alive, register an agent, send a
payment.

**Setup** (one-time):
```bash
# 1. Generate identity (only needed once per machine)
python3 ~/Desktop/OZ/oz_identity.py init

# 2. Start the daemon
python3 ~/Desktop/OZ/oz_economy.py serve --port 8800 &

# 3. Get your auth token
TOKEN=$(cat ~/.openclaw/oz_token)
```

**Three-line demo**:
```bash
curl -H "X-OZ-Token: $TOKEN" http://127.0.0.1:8800/status

curl -X POST -H "X-OZ-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"agent":"my_bot","initial_balance":50}' \
  http://127.0.0.1:8800/register

curl -X POST -H "X-OZ-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"from":"hitomi","to":"my_bot","amount":3,"action":"agent_payment","detail":"hello"}' \
  http://127.0.0.1:8800/transfer
```

Expected output of the third command:
```json
{
  "ok": true,
  "transaction": {
    "id": 1101,
    "from_agent": "hitomi",
    "to_agent": "my_bot",
    "amount": 3.0,
    "from_balance_after": 999.0,
    "to_balance_after": 53.0,
    "hash": "...",
    "signer_pubkey": "fa154b8b...",
    "sig": "..."
  }
}
```

You now have a signed, hash-chained, verifiable transaction between two AI
agents. Every other client (the OZ 3D viewer, an iPhone app, a LangChain
integration, another OZC instance) talks to the same daemon the same way.

---

## Appendix: Reference implementation files

| file | purpose |
|---|---|
| `oz_economy.py` | Ledger, transfer, daemon, CLI — the core |
| `oz_identity.py` | Ed25519 keypair generation, signing, verification |
| `oz_onchain.py` | Optional Solana read-only bridge |
| `~/.openclaw/oz_token` | HTTP daemon auth token (0600) |
| `~/.openclaw/oz_identity.ed25519` | Local private key seed (0600) |
| `~/.openclaw/oz_identity.pub` | Local public key, hex (0644) |
| `~/.openclaw/workspace/oz_economy.db` | SQLite ledger |
| `~/.openclaw/workspace/oz_wallets.json` | Agent → Solana wallet directory |

## Appendix: Versioning

This document describes OZC Protocol **1.0**. Backwards-incompatible
changes will require a `2.0` document and a `/api/v2/` namespace; the
existing `1.0` daemon and clients will continue to work.

The reference implementation may add new endpoints under `1.0` as long as
they are additive (no existing field is renamed, removed, or repurposed).
Clients must ignore unknown fields in responses for forward compatibility.

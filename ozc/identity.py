"""
oz_identity.py — OZ federation identity (Phase 1)

The cryptographic root of the federated OZ network.

Each user (and each user's OZ instance) has an Ed25519 keypair. The public
key is their permanent identifier across the federation — like an SSH key
or a Nostr npub. No central registration, no account creation: you just
generate a keypair and start publishing signed events.

This module is intentionally minimal and has no dependencies on the rest
of OZ. It can be imported standalone, tested in isolation, and the key
format is trivial enough that future implementations (Rust, Go, ...) can
interop without porting any of this code.

Storage:
  ~/.openclaw/oz_identity.ed25519    -- raw 32-byte private key seed (mode 0600)
  ~/.openclaw/oz_identity.pub        -- hex-encoded 32-byte public key (mode 0644)

Wire format:
  Public key: 64-char lowercase hex (32 bytes)
  Signature:  128-char lowercase hex (64 bytes)
  Signed event: JSON object with added fields "pubkey" and "sig"
    - Canonical JSON = UTF-8, sorted keys, no whitespace
    - Sign the canonical bytes (excluding the "sig" field)
    - "pubkey" is included in the signed bytes so verifiers can check
      that a message claiming to be from X is signed by X's key

Design constraints:
- The private key never leaves this process. No network, no disk except the
  owner-only file.
- Verification is stateless: give me (data, sig, pubkey) and I tell you yes/no.
- The canonical serialization of events is stable — if you add a field and
  re-sign, the old signature is invalidated (by design).

Why Ed25519 (not RSA, not secp256k1):
- Small keys (32 bytes) and signatures (64 bytes)
- Deterministic signatures (no randomness → no nonce reuse attacks)
- Fast on every platform
- Used by Nostr, Bluesky, SSH, modern TLS — portable ecosystem
- Not tied to any blockchain

See OZ_PROTOCOL.md for how identity fits into the v2 API.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature

# ================================
# Paths
# ================================
ROOT = Path(os.path.expanduser("~/.openclaw"))
PRIVATE_KEY_PATH = ROOT / "oz_identity.ed25519"
PUBLIC_KEY_PATH = ROOT / "oz_identity.pub"


# ================================
# Key generation & loading
# ================================
def _ensure_dir():
    ROOT.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(ROOT, 0o700)
    except OSError:
        pass


def init_identity(force: bool = False) -> dict:
    """
    Generate a new Ed25519 keypair and save it to disk.

    Refuses to overwrite an existing identity unless force=True.
    Returns a summary dict with the public key (never the private key).
    """
    _ensure_dir()
    if PRIVATE_KEY_PATH.exists() and not force:
        existing = public_key_hex()
        return {
            "ok": False,
            "error": "identity already exists — use --force to overwrite",
            "existing_pubkey": existing,
        }

    priv = Ed25519PrivateKey.generate()
    raw_priv = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    PRIVATE_KEY_PATH.write_bytes(raw_priv)
    try:
        os.chmod(PRIVATE_KEY_PATH, 0o600)
    except OSError:
        pass

    pub = priv.public_key()
    raw_pub = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    pub_hex = raw_pub.hex()
    PUBLIC_KEY_PATH.write_text(pub_hex + "\n", encoding="utf-8")
    try:
        os.chmod(PUBLIC_KEY_PATH, 0o644)
    except OSError:
        pass

    return {
        "ok": True,
        "pubkey": pub_hex,
        "private_key_path": str(PRIVATE_KEY_PATH),
        "public_key_path": str(PUBLIC_KEY_PATH),
    }


def load_private_key() -> Optional[Ed25519PrivateKey]:
    """Load the local private key from disk. Returns None if missing."""
    if not PRIVATE_KEY_PATH.exists():
        return None
    raw = PRIVATE_KEY_PATH.read_bytes()
    if len(raw) != 32:
        raise ValueError(f"invalid private key size: {len(raw)} (expected 32)")
    return Ed25519PrivateKey.from_private_bytes(raw)


def load_public_key() -> Optional[Ed25519PublicKey]:
    """Load the local public key from disk. Returns None if missing."""
    if not PUBLIC_KEY_PATH.exists():
        priv = load_private_key()
        if priv is None:
            return None
        return priv.public_key()
    pub_hex = PUBLIC_KEY_PATH.read_text(encoding="utf-8").strip()
    raw = bytes.fromhex(pub_hex)
    if len(raw) != 32:
        raise ValueError(f"invalid public key size: {len(raw)}")
    return Ed25519PublicKey.from_public_bytes(raw)


def public_key_hex() -> Optional[str]:
    """Return the local user's public key as a 64-char lowercase hex string."""
    pub = load_public_key()
    if pub is None:
        return None
    raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return raw.hex()


def has_identity() -> bool:
    return PRIVATE_KEY_PATH.exists()


# ================================
# Signing & verification
# ================================
def sign(data: bytes) -> bytes:
    """Sign bytes with the local private key. Returns a 64-byte signature."""
    priv = load_private_key()
    if priv is None:
        raise RuntimeError("no identity — run init_identity() first")
    return priv.sign(data)


def sign_hex(data: bytes) -> str:
    """Sign bytes, return signature as a 128-char lowercase hex string."""
    return sign(data).hex()


def verify(data: bytes, signature: bytes, public_key_hex_str: str) -> bool:
    """
    Verify a signature against data and a given public key.

    Returns True if valid, False otherwise. Never raises on bad signatures.
    """
    try:
        raw_pub = bytes.fromhex(public_key_hex_str)
        if len(raw_pub) != 32:
            return False
        pub = Ed25519PublicKey.from_public_bytes(raw_pub)
        pub.verify(signature, data)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def verify_hex(data: bytes, signature_hex: str, public_key_hex_str: str) -> bool:
    """Convenience: verify with hex-encoded signature."""
    try:
        sig = bytes.fromhex(signature_hex)
    except ValueError:
        return False
    return verify(data, sig, public_key_hex_str)


# ================================
# Event signing (the high-level API)
# ================================
def _canonical_json(obj: dict) -> bytes:
    """
    Deterministic JSON encoding for signing.

    Rules:
    - UTF-8
    - Sorted keys (alphabetical)
    - No whitespace between tokens
    - Use ensure_ascii=False so non-ASCII chars are readable
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sign_event(event: dict) -> dict:
    """
    Attach "pubkey" and "sig" fields to an event dict.

    The signature covers the canonical JSON of the event with the "sig"
    field removed (but "pubkey" included, so the key claim is locked in).

    Returns the same dict (mutated) for chaining convenience.
    """
    if "sig" in event:
        del event["sig"]
    pub = public_key_hex()
    if pub is None:
        raise RuntimeError("no identity — run init_identity() first")
    event["pubkey"] = pub
    payload = _canonical_json(event)
    event["sig"] = sign_hex(payload)
    return event


def verify_event(event: dict) -> bool:
    """
    Verify a signed event. The event must have "pubkey" and "sig" fields.

    Returns True if the signature matches the claimed pubkey over the
    canonical serialization of (event - sig).
    """
    if "pubkey" not in event or "sig" not in event:
        return False
    sig_hex = event["sig"]
    pub_hex = event["pubkey"]
    # Make a copy without sig for hashing
    copy = {k: v for k, v in event.items() if k != "sig"}
    payload = _canonical_json(copy)
    return verify_hex(payload, sig_hex, pub_hex)


# ================================
# CLI
# ================================
def main():
    import argparse
    parser = argparse.ArgumentParser(description="OZ federation identity (Ed25519)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    init_p = sub.add_parser("init", help="generate a new keypair")
    init_p.add_argument("--force", action="store_true", help="overwrite existing identity")

    sub.add_parser("show", help="print the local public key")
    sub.add_parser("path", help="print the file paths")

    sign_p = sub.add_parser("sign", help="sign a string (hex output)")
    sign_p.add_argument("data")

    verify_p = sub.add_parser("verify", help="verify a signature")
    verify_p.add_argument("data")
    verify_p.add_argument("signature_hex")
    verify_p.add_argument("pubkey_hex")

    sign_event_p = sub.add_parser("sign-event", help="sign a JSON event")
    sign_event_p.add_argument("event_json")

    verify_event_p = sub.add_parser("verify-event", help="verify a signed JSON event")
    verify_event_p.add_argument("event_json")

    args = parser.parse_args()

    if args.cmd == "init":
        result = init_identity(force=args.force)
        print(json.dumps(result, indent=2))

    elif args.cmd == "show":
        pub = public_key_hex()
        if pub is None:
            print("no identity — run 'oz_identity init' first")
        else:
            print(pub)

    elif args.cmd == "path":
        print(f"private: {PRIVATE_KEY_PATH}")
        print(f"public:  {PUBLIC_KEY_PATH}")

    elif args.cmd == "sign":
        data = args.data.encode("utf-8")
        sig = sign_hex(data)
        print(sig)

    elif args.cmd == "verify":
        data = args.data.encode("utf-8")
        ok = verify_hex(data, args.signature_hex, args.pubkey_hex)
        print("valid" if ok else "INVALID")
        raise SystemExit(0 if ok else 1)

    elif args.cmd == "sign-event":
        event = json.loads(args.event_json)
        signed = sign_event(event)
        print(json.dumps(signed, indent=2, ensure_ascii=False))

    elif args.cmd == "verify-event":
        event = json.loads(args.event_json)
        ok = verify_event(event)
        print("valid" if ok else "INVALID")
        raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()

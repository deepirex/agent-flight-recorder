"""Merkle trees over the recorder's entry hashes, in the style of Certificate Transparency (RFC 9162).

One root fingerprints the whole log. An inclusion proof shows that a single entry is part of a published root while
revealing nothing about any other entry, so an auditor can check one decision without seeing anyone else's data.
"""

from __future__ import annotations

import hashlib


def leaf_hash(entry_hash_hex: str) -> bytes:
    return hashlib.sha256(b"\x00" + bytes.fromhex(entry_hash_hex)).digest()


def _node(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _split(n: int) -> int:
    k = 1
    while k * 2 < n:
        k *= 2
    return k


def root(leaves: list[bytes]) -> bytes:
    n = len(leaves)
    if n == 0:
        return hashlib.sha256(b"").digest()
    if n == 1:
        return leaves[0]
    k = _split(n)
    return _node(root(leaves[:k]), root(leaves[k:]))


def inclusion_path(index: int, leaves: list[bytes]) -> list[bytes]:
    n = len(leaves)
    if n <= 1:
        return []
    k = _split(n)
    if index < k:
        return inclusion_path(index, leaves[:k]) + [root(leaves[k:])]
    return inclusion_path(index - k, leaves[k:]) + [root(leaves[:k])]


def verify_inclusion(leaf: bytes, index: int, size: int, path: list[bytes], expected_root: bytes) -> bool:
    """RFC 9162, section 2.1.3.2."""
    if index >= size:
        return False
    fn, sn, r = index, size - 1, leaf
    for p in path:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            r = _node(p, r)
            if not fn & 1:
                while not fn & 1 and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            r = _node(r, p)
        fn >>= 1
        sn >>= 1
    return sn == 0 and r == expected_root

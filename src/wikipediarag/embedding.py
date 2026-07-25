from __future__ import annotations

import hashlib
import math


def embed_text(text: str, dimensions: int = 64) -> list[float]:
    vector = [0.0] * dimensions
    tokens = [token for token in normalize_for_embedding(text).split(" ") if token]
    if not tokens:
        return vector
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[bucket] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [round(value / norm, 6) for value in vector]


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True))


def normalize_for_embedding(text: str) -> str:
    lowered = text.casefold()
    chars: list[str] = []
    for char in lowered:
        chars.append(char if char.isalnum() else " ")
    return " ".join("".join(chars).split())

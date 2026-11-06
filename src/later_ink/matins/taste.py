"""What a catalogue likes, learnt from what its reader did, and how close an
article is to it.

The signals are the ones the curator already reads from /api/v1/signals: a like
or "not for me" (±1), opening the original (+0.7), reading in the Reader
(+0.35); an explicit mark wins for the same link. Each signal's headline becomes
a vector (embed.py) and older signals count for less (half-life 30 days).

An article's score is its similarity to the closest things the reader liked,
minus half its similarity to the closest things they did not: nearest
neighbours rather than one average, because a reader who likes e-ink and Azure
is not interested in their midpoint. Scores fall roughly in -0.5..1; only their
order matters.

Nothing here runs without the embedding model (embed.embedder() is None) or with
too few signals to say anything (MIN_POSITIVE): the caller keeps its own order.
"""

import time
from dataclasses import dataclass

import numpy as np

from . import embed

HALF_LIFE_DAYS = 30.0
MIN_POSITIVE = 3
NEIGHBOURS = 5
DISLIKE_WEIGHT = 0.5
# A profile is rebuilt when the signals change, and at most this often anyway.
_TTL = 300.0


def weighted_signals(store, secret: str) -> list[dict]:
    """Everything the reader expressed, explicit and implicit, newest first."""
    rows: dict[str, dict] = {}
    for signal in store.list_signals(secret, limit=2000):
        positive = signal.rating > 0
        rows[signal.url] = {
            "url": signal.url,
            "title": signal.title,
            "source": signal.source,
            "rating": "interesting" if positive else "not_interested",
            "weight": 1.0 if positive else -1.0,
            "kind": "like" if positive else "dislike",
            "updated_at": int(signal.updated_at),
        }
    best: dict[str, tuple[float, object]] = {}
    for event in store.list_events(secret, limit=3000):
        weight = {"read": 0.35, "opened": 0.7}.get(event.kind, 0.0)
        if weight <= 0:
            continue
        current = best.get(event.url)
        if current is None or weight > current[0]:
            best[event.url] = (weight, event)
    for url, (weight, event) in best.items():
        if url in rows:
            continue
        rows[url] = {
            "url": url,
            "title": getattr(event, "title", ""),
            "source": getattr(event, "source", ""),
            "rating": "interesting",
            "weight": weight,
            "kind": getattr(event, "kind", "read"),
            "updated_at": int(getattr(event, "created_at", 0)),
        }
    return sorted(rows.values(), key=lambda row: row["updated_at"], reverse=True)


@dataclass
class Taste:
    liked: np.ndarray  # n × dim, unit rows
    liked_weight: np.ndarray  # n
    disliked: np.ndarray
    disliked_weight: np.ndarray
    signals: int

    def score_vector(self, vector: np.ndarray) -> float:
        score = _nearest(self.liked, self.liked_weight, vector)
        if len(self.disliked):
            score -= DISLIKE_WEIGHT * _nearest(self.disliked, self.disliked_weight, vector)
        return score

    def score(self, text: str) -> float | None:
        vector = embed.vector(" ".join(text.split())[: embed.MAX_CHARS])
        return None if vector is None else self.score_vector(vector)


def _nearest(matrix: np.ndarray, weights: np.ndarray, vector: np.ndarray) -> float:
    sims = matrix @ vector
    k = min(NEIGHBOURS, len(sims))
    top = np.argpartition(-sims, k - 1)[:k]
    return float((sims[top] * weights[top]).sum() / weights[top].sum())


_cache: dict[str, tuple[float, tuple, Taste | None]] = {}


def taste_for(store, secret: str, now: float | None = None) -> Taste | None:
    """The catalogue's taste, or None (no model, or not enough signals yet)."""
    if embed.embedder() is None:
        return None
    now = now or time.time()
    rows = weighted_signals(store, secret)
    stamp = (len(rows), rows[0]["updated_at"] if rows else 0)
    hit = _cache.get(secret)
    if hit and hit[1] == stamp and now - hit[0] < _TTL:
        return hit[2]
    liked, liked_w, disliked, disliked_w = [], [], [], []
    for row in rows:
        title = " ".join((row.get("title") or "").split())
        vector = embed.vector(title) if title else None
        if vector is None:
            continue
        age_days = max(0.0, (now - row["updated_at"]) / 86400)
        weight = row["weight"] * 0.5 ** (age_days / HALF_LIFE_DAYS)
        if weight > 0:
            liked.append(vector)
            liked_w.append(weight)
        elif weight < 0:
            disliked.append(vector)
            disliked_w.append(-weight)
    taste = None
    if len(liked) >= MIN_POSITIVE:
        dim = liked[0].shape[0]
        taste = Taste(
            liked=np.vstack(liked),
            liked_weight=np.asarray(liked_w, dtype=np.float32),
            disliked=np.vstack(disliked) if disliked else np.zeros((0, dim), np.float32),
            disliked_weight=np.asarray(disliked_w, dtype=np.float32),
            signals=len(rows),
        )
    _cache[secret] = (now, stamp, taste)
    return taste


def article_text(title: str, summary: str = "") -> str:
    """What an article is about, for scoring: its headline, then its lede."""
    return f"{title}. {summary}" if summary else title

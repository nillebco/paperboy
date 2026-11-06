"""Article vectors for "more like what I read", without a neural runtime.

The model is potion-multilingual-128M (model2vec, MIT): a static embedding,
one 256-dimension vector per token of the bge-m3 vocabulary. An article's
vector is the normalised mean of its tokens' vectors, so all it takes is the
tokenizer and a matrix lookup — numpy, and the SentencePiece Unigram tokenizer
below. The matrix (~512 MB) is memory-mapped, not loaded.

Fetched at image build by scripts/vendor_embed.py. Without it, recommendation
is off and everything keeps its plain order.

The tokenizer is bge-m3's Unigram model, run with NFKC where the original uses
a precompiled character map: close enough for an average of token vectors,
which is what ranks articles here, not a byte-exact reimplementation.
"""

import json
import logging
import math
import threading
import unicodedata
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import config

logger = logging.getLogger(__name__)

_SPACE = "▁"
# The tokenizer's normaliser puts spaces around ASCII punctuation.
_PUNCT = str.maketrans({c: f" {c} " for c in "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"})
# Enough of an article to say what it is about; the mean of more tokens
# drifts toward the language's common words.
MAX_CHARS = 1200


class Embedder:
    def __init__(self, root: Path) -> None:
        tokenizer = json.loads((root / "tokenizer.json").read_text(encoding="utf-8"))
        model = tokenizer["model"]
        self._unk = model["unk_id"]
        self._pieces: dict[str, tuple[int, float]] = {}
        for index, (piece, score) in enumerate(model["vocab"]):
            self._pieces[piece] = (index, score)
        self._longest = max(len(piece) for piece in self._pieces)
        self._unk_score = min(score for _, score in self._pieces.values()) - 10.0
        self._matrix = _load_safetensors(root / "model.safetensors")

    @property
    def dim(self) -> int:
        return int(self._matrix.shape[1])

    def tokens(self, text: str) -> list[int]:
        """Unigram (Viterbi) segmentation of `text` into vocabulary ids."""
        text = " ".join(unicodedata.normalize("NFKC", text).translate(_PUNCT).split())
        if not text:
            return []
        text = _SPACE + text.replace(" ", _SPACE)
        n = len(text)
        best = [-math.inf] * (n + 1)
        back: list[tuple[int, int]] = [(0, self._unk)] * (n + 1)
        best[0] = 0.0
        for end in range(1, n + 1):
            for start in range(max(0, end - self._longest), end):
                if best[start] == -math.inf:
                    continue
                hit = self._pieces.get(text[start:end])
                if hit is not None:
                    score = best[start] + hit[1]
                    if score > best[end]:
                        best[end], back[end] = score, (start, hit[0])
            if best[end] == -math.inf:
                # A character the vocabulary does not have: one unknown token.
                best[end], back[end] = best[end - 1] + self._unk_score, (end - 1, self._unk)
        ids = []
        end = n
        while end > 0:
            start, token = back[end]
            ids.append(token)
            end = start
        ids.reverse()
        return ids

    def encode(self, text: str) -> np.ndarray | None:
        ids = [i for i in self.tokens(text[:MAX_CHARS]) if i != self._unk]
        if not ids:
            return None
        vector = np.asarray(self._matrix[ids], dtype=np.float32).mean(axis=0)
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm else None


def _load_safetensors(path: Path) -> np.ndarray:
    """The one tensor of a safetensors file, memory-mapped (no safetensors package)."""
    with path.open("rb") as f:
        size = int.from_bytes(f.read(8), "little")
        header = json.loads(f.read(size))
    name = next(key for key in header if key != "__metadata__")
    info = header[name]
    dtypes = {"F32": np.float32, "F16": np.float16}
    if info["dtype"] not in dtypes:
        raise ValueError(f"unsupported dtype {info['dtype']}")
    start, end = info["data_offsets"]
    rows, cols = info["shape"]
    data = np.memmap(path, dtype=dtypes[info["dtype"]], mode="r", offset=8 + size + start,
                     shape=(rows, cols))
    if (end - start) != data.nbytes:
        raise ValueError("safetensors size mismatch")
    return data


_lock = threading.Lock()
_loaded: dict[str, Embedder | None] = {}


def embedder() -> Embedder | None:
    """The shared embedder, or None when the model is not installed."""
    root = config.get_embed_model_dir()
    if root is None:
        return None
    key = str(root)
    with _lock:
        if key not in _loaded:
            try:
                _loaded[key] = Embedder(root)
            except (OSError, ValueError, KeyError) as e:
                logger.warning("embedding model unusable: %s", e)
                _loaded[key] = None
        return _loaded[key]


@lru_cache(maxsize=4096)
def vector(text: str) -> np.ndarray | None:
    """`text`'s vector, cached: the same headlines come back on every page."""
    model = embedder()
    return model.encode(text) if model else None

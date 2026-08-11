"""Text embedding, with swappable backends.

The pipeline only ever needs one thing from an embedder: turn text into a unit
vector whose cosine similarity tracks "these say the same thing". Everything
downstream — the index, the similarity bands, dedup — is written against that
contract, so the backend can be upgraded without touching any of it.

Two backends:

  LexicalEmbedder  (default, zero dependencies beyond numpy)
      Hashed character 4-grams plus word unigrams. Catches near-duplicates,
      quotes, light rewordings, and copy-paste — which is the bulk of real
      repetition on a timeline. It cannot tell that "RLHF is saturating" and
      "preference tuning has hit diminishing returns" are the same claim.

  NeuralEmbedder   (slot, not yet installed)
      A sentence-transformer would close that gap. Deliberately not installed
      here: torch is ~2.5GB and this machine's disk is 88% full. Swap it in by
      implementing encode() and changing DEFAULT.

The honest framing: lexical handles the high-similarity band well, which is
where most duplicate volume lives. Genuine paraphrase detection waits for a
neural backend.
"""

from __future__ import annotations

import hashlib
import os
import re

import numpy as np

DIM = 2048
_TOKEN_RE = re.compile(r"[a-z0-9']+")
_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"@\w+")


def normalise(text: str) -> str:
    """Strip the parts that differ between reposts of the same content.

    URLs get shortened per-post by t.co, and leading mentions vary by who is
    being replied to — neither says anything about what the post means.
    """
    text = text.lower()
    text = _URL_RE.sub(" ", text)
    text = _MENTION_RE.sub(" ", text)
    return " ".join(text.split())


MIN_TOKENS = 3

# Chinese, Japanese and Korean do not put spaces between words, so a
# word-token count reads every CJK post as empty. This guard drops posts with
# "no text", which meant a Weibo post of four dense sentences was binned as a
# link-only retweet and never reached dedup or the judge at all. Counting han
# characters individually is crude but right in the direction that matters:
# each one carries roughly a morpheme.
_CJK_RE = re.compile(
    r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]")


def token_count(text: str) -> int:
    clean = normalise(text)
    return len(_TOKEN_RE.findall(clean)) + len(_CJK_RE.findall(clean))


def has_signal(text: str) -> bool:
    """Does this post contain enough words to compare meaningfully?

    A post that is just a mention plus a t.co link normalises to the empty
    string, and empty vectors match each OTHER at 1.000 — so without this guard
    every link-only retweet gets deduped against every other unrelated one.
    """
    return token_count(text) >= MIN_TOKENS


def _hash(token: str) -> int:
    return int.from_bytes(hashlib.blake2b(token.encode(), digest_size=4).digest(), "big") % DIM


class LexicalEmbedder:
    name = "lexical-hash-v1"
    dim = DIM

    def encode(self, text: str) -> np.ndarray:
        clean = normalise(text)
        vec = np.zeros(DIM, dtype=np.float32)
        if not clean:
            return vec

        # Word unigrams carry topic; character 4-grams carry phrasing and
        # survive typos, inflection, and truncation.
        for word in _TOKEN_RE.findall(clean):
            vec[_hash("w:" + word)] += 1.0
        padded = f" {clean} "
        for i in range(len(padded) - 3):
            vec[_hash("c:" + padded[i:i + 4])] += 0.5

        norm = np.linalg.norm(vec)
        return vec / norm if norm else vec

    def encode_many(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, DIM), dtype=np.float32)
        return np.vstack([self.encode(t) for t in texts])


# The English model is blind to Chinese, and not in a way that fails safely.
# Measured on potion-base-8M: two unrelated Chinese sentences scored 0.480 —
# above the search threshold and near the clustering one — while a genuine
# Chinese paraphrase pair scored exactly 0.000, both words being out of
# vocabulary. Unrelated posts would merge and real duplicates would not.
#
# potion-multilingual-128M fixes it (unrelated 0.158, paraphrase 0.678) and
# barely moves English (0.800 vs 0.839). It costs 1GB on disk against 59MB,
# which is why it is a choice rather than the default: most users track English
# sources only and should not pay for it.
#
#   AI_SIGNAL_EMBED=multilingual
#
# Switching re-embeds the corpus on the next dedup — local CPU work, no API
# cost. Vectors are keyed by model name, so the two never mix.
MULTILINGUAL = os.environ.get("AI_SIGNAL_EMBED", "").lower().startswith("multi")


class SemanticEmbedder:
    """Distilled static embeddings (model2vec) — semantics without torch.

    ~30MB and CPU-only, versus ~2.5GB for torch + sentence-transformers, because
    the model is distilled down to a static token->vector table. That costs some
    accuracy against a full transformer, but it closes the paraphrase gap that
    the lexical backend cannot touch at all: measured 0.831 vs 0.591 on a true
    paraphrase pair.
    """

    repo = ("minishlab/potion-multilingual-128M" if MULTILINGUAL
            else "minishlab/potion-base-8M")
    name = repo.split("/")[-1]
    dim = 256
    _model = None

    def _load(self):
        if SemanticEmbedder._model is None:
            from model2vec import StaticModel
            SemanticEmbedder._model = StaticModel.from_pretrained(self.repo)
        return SemanticEmbedder._model

    def encode(self, text: str) -> np.ndarray:
        return self.encode_many([text])[0]

    def encode_many(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        cleaned = [normalise(t) or " " for t in texts]
        vecs = np.asarray(self._load().encode(cleaned), dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vecs / norms


# Every post is embedded by both backends and similarity is the max of the two.
# Neither dominates: lexical is stronger on typos and copy-paste, semantic is
# stronger on paraphrase. Taking the max means a pair only has to look alike to
# ONE of them to be flagged, which is the behaviour we want for dedup.
BACKENDS = [LexicalEmbedder(), SemanticEmbedder()]
DEFAULT = BACKENDS[0]


def to_blob(vec: np.ndarray) -> bytes:
    return vec.astype(np.float32).tobytes()


def from_blob(blob: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32, count=dim)

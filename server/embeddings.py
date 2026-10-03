"""Embeddings (bge-small, 384-d) and cross-encoder reranking, both ONNX via fastembed, CPU-only."""
import os
import threading

import numpy as np

EMBED_MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-small-en-v1.5")
RERANK_MODEL = os.environ.get("RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2")
DIM = 384

_lock = threading.Lock()
_embedder = None
_reranker = None


def _get_embedder():
    global _embedder
    with _lock:
        if _embedder is None:
            from fastembed import TextEmbedding
            _embedder = TextEmbedding(EMBED_MODEL)
        return _embedder


def _get_reranker():
    global _reranker
    with _lock:
        if _reranker is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
            _reranker = TextCrossEncoder(RERANK_MODEL)
        return _reranker


def _blob(vec) -> bytes:
    v = np.asarray(vec, dtype=np.float32)
    return (v / (np.linalg.norm(v) or 1.0)).tobytes()


def embed_doc(text: str) -> bytes:
    return _blob(next(iter(_get_embedder().embed([text]))))


def embed_docs(texts: list[str]) -> list[bytes]:
    return [_blob(v) for v in _get_embedder().embed(texts)]


def embed_query(text: str) -> bytes:
    # bge uses an instruction prefix for queries; fastembed's query_embed applies the model's convention.
    return _blob(next(iter(_get_embedder().query_embed(text))))


def rerank(query: str, docs: list[str]) -> list[float]:
    if not docs:
        return []
    return [float(s) for s in _get_reranker().rerank(query, docs)]


def warm(rerank_too: bool = True) -> None:
    """Load models at startup so the first real request isn't a 10+ s cold start (seen in v1)."""
    embed_query("warm up")
    if rerank_too:
        rerank("warm up", ["warm up"])

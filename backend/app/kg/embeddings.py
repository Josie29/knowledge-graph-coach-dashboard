"""Local embedding support for the concept vector index (resolver pass 3).

The KG build script embeds every ``Concept`` node's labels at build time, and the
three-pass resolver embeds query text at request time. Both must use the same
model or the cosine scores are meaningless, so the model name and dimension live
here and nowhere else. fastembed runs a local ONNX model — no API key, no
network after the first model download (see docs/tech-stack.md).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Iterable

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384


@lru_cache(maxsize=1)
def _model() -> Any:
    # Imported lazily: fastembed pulls in onnxruntime, which the API only needs
    # when the resolver's vector pass actually runs. Cached because model load
    # costs ~a second and the resolver runs per request.
    from fastembed import TextEmbedding

    return TextEmbedding(EMBEDDING_MODEL)


def embed_texts(texts: Iterable[str]) -> list[list[float]]:
    """Embed texts with the project's pinned local model.

    Args:
        texts: Strings to embed.

    Returns:
        One ``EMBEDDING_DIM``-length vector per input text, in order.
    """
    return [vector.tolist() for vector in _model().embed(list(texts))]

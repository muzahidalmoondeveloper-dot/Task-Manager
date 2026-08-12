"""Provider-agnostic embedding + cosine-similarity helpers for the
Knowledge/RAG retrieval domain (architecture item 1).

Real embeddings — not a placeholder: this deployment's configured
provider (Ollama, via LLMGateway.embed()) is backed by the
`nomic-embed-text` model, confirmed working end-to-end in this
environment (`ollama pull nomic-embed-text`, verified via a direct
`/api/embed` call returning real 768-dimensional float vectors).

Vectors are stored as plain JSON float arrays and compared with a
Python-computed cosine similarity rather than a native Postgres vector
column with an ANN index, because pgvector is confirmed unavailable in
this environment (`CREATE EXTENSION vector` fails with
FeatureNotSupportedError — no OS-level compiled binary, no admin access
to install one). This is the honest, bounded shape of "vector search"
available here: correct similarity ranking, without approximate-nearest-
neighbor indexing at scale. Documented as the one genuine infra-blocked
corner of the spec's retrieval requirement.

Provider-agnostic: nothing here is Ollama-specific — any LLMProvider that
implements `embed()` works through the exact same path (get_llm_provider()
already returns whichever provider LLM_PROVIDER selects); providers that
don't implement it raise NotImplementedError, which callers here catch and
propagate as `None` so search_knowledge can degrade to keyword-only
ranking instead of crashing the whole search.
"""

import logging
import math

logger = logging.getLogger("copilot.embeddings")


async def embed_text(text: str) -> tuple[list[float] | None, str | None]:
    """Returns (embedding, model_name) or (None, None) if the configured
    LLM provider has no embedding capability, or the embedding call itself
    fails for any reason (network, provider error) — never raises, since a
    missing/failed embedding must degrade a caller to keyword-only search,
    not break it."""
    from app.services.llm import get_llm_provider

    provider = get_llm_provider()
    try:
        vectors = await provider.embed([text], capability="embeddings")
    except NotImplementedError:
        logger.info("embed_text: configured LLM provider has no embedding capability — degrading to keyword-only")
        return None, None
    except Exception as exc:
        logger.warning("embed_text: embedding call failed (%s) — degrading to keyword-only", exc)
        return None, None

    if not vectors:
        return None, None

    model_name = getattr(provider, "_embed_model", None) or getattr(provider, "_default_model", None) or "unknown"
    return vectors[0], model_name


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Standard cosine similarity in [-1, 1]; 0.0 for degenerate (zero-norm
    or mismatched-length) vectors rather than raising — a corrupted/partial
    stored embedding must never crash ranking, just score as "no match"."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def normalize_scores(scores: list[float]) -> list[float]:
    """Min-max normalize to [0, 1] so keyword scores (pg_trgm/ts_rank,
    small magnitudes) and vector scores (cosine, roughly [0, 1] already)
    can be fused on a comparable scale — a fixed-weight sum of two
    differently-scaled signals would let whichever happens to have the
    larger raw magnitude dominate the ranking regardless of relevance."""
    if not scores:
        return []
    lo, hi = min(scores), max(scores)
    if hi - lo < 1e-9:
        return [1.0 for _ in scores]
    return [(s - lo) / (hi - lo) for s in scores]

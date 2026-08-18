# OpenAI embedding provider restores hybrid retrieval

> Tracking issue: [#74](https://github.com/Wmx-5Percent/FDAgent/issues/74).
> Status snapshot: 2026-08-06. This is a provider-decision and implementation
> handoff plan for restoring true pgvector semantic retrieval on `main`.

## Problem

Hybrid retrieval had been marked as restored, but runtime logs still showed semantic
queries degrading to `retrieval_mode=fts_only`. The symptom was visible in both
`query_log` and `hybrid_search_log`: query embeddings failed before pgvector search
could run, so the retrieval pipeline correctly fell back to keyword FTS.

This is not an `embeddings` table coverage issue. The current local database has
35,446 populated vectors for `drug_enforcement`, covering all 17,723 records across
`reason_for_recall` and `product_description`.

## Findings

- Runtime `.env` previously routed query embeddings through `EMBED_PROVIDER=openai`,
  while the OpenAI account had quota/auth failures; logs therefore showed
  `ProviderQuotaError(provider=openai, model=text-embedding-3-small, operation=embedding)`.
- Switching query embeddings to OpenRouter with `openai/text-embedding-3-small`
  did not fix the issue for this account. OpenRouter key metadata was readable and
  non-OpenAI embedding models could run, but OpenRouter rejected OpenAI-family
  embedding calls with HTTP 403:
  `The request is prohibited due to a violation of provider Terms Of Service`.
- Direct OpenAI embeddings now work with the configured `OPENAI_API_KEY`.
  A direct `text-embedding-3-small` probe returned a 1536-dimensional vector, which
  matches the corpus vectors already stored in pgvector.
- Forcing the project retrieval path to direct OpenAI restored true hybrid mode:

  ```bash
  EMBED_PROVIDER=openai EMBED_MODEL=text-embedding-3-small \
    .venv/bin/python src/retrieval.py "pills too strong" --field both -k 5
  ```

  Expected result:

  ```text
  Embedding: provider=openai model=text-embedding-3-small dimension=1536 retrieval_mode=hybrid
  ```

## Decision

Use direct OpenAI for query embeddings, and keep chat / structured-output calls on
OpenRouter.

Recommended runtime split:

```env
LLM_PROVIDER=openrouter
LLM_MODEL=deepseek/deepseek-v4-pro

EMBED_PROVIDER=openai
EMBED_MODEL=text-embedding-3-small
```

Do not switch query embeddings to Qwen, Gemini, or another OpenRouter-accessible
model unless the corpus is re-embedded into the same model space. Query vectors and
stored corpus vectors must be produced by the same embedding model family; matching
dimension alone is not enough. In the verified OpenRouter probe, `qwen/qwen3-embedding-4b`
returned 2560 dimensions, so it is immediately incompatible with the current 1536-d
pgvector corpus.

## Implementation notes

- Ensure `.env` uses `EMBED_PROVIDER=openai` and `EMBED_MODEL=text-embedding-3-small`.
- Fully restart the FastAPI service after changing provider settings. `NLEngine`
  creates and caches the embedding client at startup.
- Use `/health` only as a coarse configuration check for now. A future follow-up
  should add a real embedding probe so `embed_available` means "embedding API call
  succeeded", not only "client construction succeeded".
- Keep current fallback behavior: if embedding fails, retrieval should remain explicit
  `fts_only` with an `embedding_fallback_reason`; do not silently report keyword-only
  misses as semantic zero results.

## Validation

Run the direct provider probe:

```bash
.venv/bin/python - <<'PY'
import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(".env")
client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
resp = client.embeddings.create(
    model="text-embedding-3-small",
    input=["FDA drug recall semantic retrieval probe"],
)
print(len(resp.data[0].embedding))
PY
```

Expected output:

```text
1536
```

Run retrieval through the project path:

```bash
EMBED_PROVIDER=openai EMBED_MODEL=text-embedding-3-small \
  .venv/bin/python src/retrieval.py "pills too strong" --field both -k 5
```

Acceptance criteria:

- output includes `retrieval_mode=hybrid`;
- returned rows include vector/semantic matches such as superpotent or oversized-tablet
  recall language;
- latest `hybrid_search_log` entries for the same query should have
  `retrieval_mode='hybrid'`, `fallback_reason IS NULL`, and `vector_hit_count > 0`
  when exercised through the API.

## Follow-ups

- Add a lightweight embedding provider health probe to `/health`.
- Update query and hybrid-search logging so `embedding_available` reflects a real
  provider call or the latest fallback state.
- If direct OpenAI embeddings become unavailable, open a separate migration issue for
  model-aware vector stores and full corpus re-embedding with a non-OpenAI model.

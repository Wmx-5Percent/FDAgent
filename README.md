# MRI Ticket Intelligence Agent

An LLM-powered system that turns **unstructured medical-device service tickets** into
structured insights, then lets you **search, ask, and analyze** them through a
retrieval-augmented agent.

> **Portfolio note.** This project reproduces — on 100% **public-domain data** — the
> same applied-AI pipeline I built in industry (large-scale LLM ticket structuring,
> vector search, and an analytics agent). No proprietary data or code is used. See
> [数据敏感性分析与合规说明.md](数据敏感性分析与合规说明.md).

---

## Data: openFDA MAUDE (public domain, FDA-de-identified)

Source: [openFDA Device Adverse Event API](https://open.fda.gov/apis/device/event/) —
U.S.-government medical-device reports. Each report has free-text narratives that are
structurally identical to service tickets (FDA already redacts PII; look for `(B)(4)`).

```bash
python3 src/fetch_openfda.py --count 2000        # MRI reports -> data/raw/
python3 src/fetch_openfda.py --count 5000 --query 'magnetic resonance'
```

Output `data/raw/tickets.csv` has a **`ticket_text`** column — the analog of a raw
ticket — which the structuring pipeline splits into:

| Structured column | Meaning |
| --- | --- |
| `issue_summary` | what failed / the reported problem |
| `troubleshooting_steps` | what was investigated |
| `solution_action` | root cause / resolution taken |

---

## Architecture / Roadmap

```
data/raw/tickets.csv  (unstructured)
   │
   ├─ [Phase 1] LLM structuring pipeline      src/structure.py   ← async + retry + JSON-schema
   │     → issue_summary / troubleshooting_steps / solution_action  + CAT_L1/CAT_L2 labels
   │
   ├─ [Phase 2] Embed + index                 src/index.py       ← embeddings → vector store
   │     → semantic "find similar tickets"
   │
   ├─ [Phase 3] Agent                         src/agent.py       ← tool-calling
   │     (a) similar-ticket retrieval   (b) RAG Q&A with citations
   │     (c) analytics / NL→stats ("top failure modes this year")
   │
   ├─ [Phase 4] Eval                          src/eval.py        ← precision/recall/F1/Cohen's κ
   │
   └─ [Phase 5] Serve                         src/api.py         ← FastAPI + minimal UI + Docker
```

**Status:** Phase 0 done (legal data + scaffold). Phase 1 next.

---

## Tech stack

Python · OpenAI/Azure OpenAI API · async batching · Pydantic (structured output) ·
vector store (Chroma/FAISS) · scikit-learn (eval) · FastAPI · Docker.

---

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # add your OPENAI_API_KEY
python3 src/fetch_openfda.py --count 2000
```

> `fetch_openfda.py` needs **no** dependencies (standard library only); the rest of the
> pipeline uses `requirements.txt`.

---

## IP safety

- Real company data (`data/free_all.xlsx`) is **git-ignored** and never published.
- All committed data is public-domain openFDA (or LLM-synthetic).

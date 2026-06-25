# Skills Index

> Reusable skills for this repo — workflows, API guides, and best practices an agent can load on demand.
> **To use a capability:** find it by category below and open its `SKILL.md`.
> **To add one:** see *Adding a skill*, and add an entry here in the same change so it stays findable.

## Categorized index

### Workflow
Complete workflows for a specific task.
- [db-column-docs-from-dictionary](db-column-docs-from-dictionary/SKILL.md) — Attach an upstream field dictionary's **verbatim** definitions to a database table as Postgres column COMMENTs, with explicit `[inferred]` markers for anything the source does not document.
- [learning-session-notes](learning-session-notes/SKILL.md) — Summarize project conversations into durable personal learning notes: concepts, interview Q&A, project examples, and strategy tradeoffs, while deduplicating against `learning-notes/INDEX.md`.

### API Guide
How to call an external system or data source.
- [openfda-data-download](openfda-data-download/SKILL.md) — Download or incrementally ingest any openFDA endpoint into a local store; includes an API-vs-bulk **size decision guide** and measured **record counts per dataset**.

### BestPractice
General methodology and lessons.
- [skill-writing](skill-writing/SKILL.md) — Meta-skill for writing or rewriting skills: result-determinism over SOP, testable acceptance criteria, clear boundaries, and pitfalls drawn from real failures (not invented).

## Adding a skill
1. **Invocable skills** live at `<name>/SKILL.md` with YAML frontmatter (`name`, `description`). The `description` is the discovery surface — put trigger phrases ("Use when …") in it, since the agent loads a skill by matching that field, not by reading this index. (A flat `*.md` that is not inside a `<name>/SKILL.md` folder is reference-only and is **not** auto-discovered.)
2. Follow [skill-writing/SKILL.md](skill-writing/SKILL.md): write goal + acceptance criteria + boundaries + real pitfalls, not a step-by-step SOP.
3. **Add a one-line entry above** under the right category. This index is hand-maintained — updating it in the same change is what keeps a new skill findable (the meta-skill flags "forgot to update INDEX" as a common pitfall).

## Progressive disclosure
This index is the overview (quick locate). Each `SKILL.md` holds the full goal, acceptance criteria, resources, and pitfalls — load a skill only when the task matches its description.

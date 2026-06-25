---
name: learning-session-notes
description: "Summarize a project conversation/session into durable personal learning notes. Use when: extracting technical concepts, interview-relevant Q&A, implementation rationale, and strategy tradeoffs from coding/architecture/brainstorming discussions; consolidating Docker, RAG, retrieval, LLM-agent, database, deployment, or evaluation learning into local notes without duplicates. Not for: routine progress updates, commit summaries, or replacing PROGRESS.md."
---

# Learning Session Notes

## Metadata

- **Type**: Workflow + BestPractice
- **Primary output**: `learning-notes/INDEX.md` and topic files under `learning-notes/`
- **Scope**: personal learning / interview review notes for this portfolio project
- **Created**: 2026-06-25

## Goal

Convert a project conversation into durable, non-duplicated learning notes that help the user review technical concepts, explain design choices, and prepare for interviews.

The output is not a transcript and not a project changelog. It should preserve the reusable knowledge from the session: what a concept means, how it works, where it appears in this project, why a strategy was chosen over alternatives, and what interview questions it prepares the user to answer.

## Boundaries

This skill should summarize learning value from three common conversation types:

- **Instructional work**: implementation/debugging/deployment sessions where the user asked the agent to do something and the exchange revealed reusable engineering lessons.
- **Discussion / design exploration**: feature ideas, architecture choices, technology selection, and tradeoff analysis.
- **Learning Q&A**: explanations of unfamiliar concepts, acronyms, tools, algorithms, infrastructure, agent behavior, observability, retrieval, databases, or deployment.

Do not use this skill for routine status tracking. `PROGRESS.md` remains the project state source of truth. Do not store secrets, proprietary company data, API keys, credentials, or raw private transcripts in `learning-notes/`.

## Required Inputs

A valid run needs access to at least one source of session evidence:

- the current conversation context,
- a session history store or debug log,
- pasted transcript excerpts from the user,
- or a user-specified time/session target.

If the target session is ambiguous and multiple sessions could match, ask the user to identify the intended session before writing notes.

## Output Location And Index Contract

All notes live under `learning-notes/`, which is intentionally git-ignored for personal study use.

`learning-notes/INDEX.md` is the routing surface for future deduplication. It must let a future agent quickly answer: "Have we already written about this topic, and if so, which file should be updated?"

Each index entry should include:

- topic title,
- topic file path,
- tags / aliases,
- last updated date,
- short coverage summary,
- whether the topic includes strategy tradeoffs and interview questions.

Topic files should use stable, readable slugs such as `docker.md`, `hybrid-search.md`, `agent-observability.md`, or `postgres-full-text-search.md`.

## Topic File Contract

A topic note is successful when it contains the relevant sections below. Omit empty sections rather than filling space.

```markdown
# <Topic Title>

> Last updated: YYYY-MM-DD
> Tags: tag1, tag2
> Aliases: optional search terms

## Core Idea
A compact explanation in the user's own learning context.

## How It Works
Mechanics, components, algorithms, data flow, or implementation model.

## FDAgent Example
How this appears, could appear, or was discussed in this project.

## Strategy Decisions
| Decision | Chosen | Not Chosen | Why | Tradeoff |
|---|---|---|---|---|

## Interview Angles
Questions the user should be able to answer after reviewing this note.

## Gotchas
Concrete pitfalls from the session or project. Do not invent speculative pitfalls.

## Follow-Up Gaps
Open questions or concepts worth revisiting later.
```

## Acceptance Criteria

A completed summary satisfies all of these checks:

- `learning-notes/INDEX.md` was read before deciding whether to create or update topic files.
- Existing topic files referenced by matching index entries were read before editing them.
- No duplicate topic file is created for the same concept; new details are merged into the existing topic note.
- The summary captures both concept explanations and project-specific strategy choices when they exist.
- Every strategy decision records the selected option, at least one rejected alternative, and the reason for the tradeoff.
- Interview-relevant concepts are phrased as answerable questions or prompts.
- Claims are grounded in the session evidence. Uncertain details are marked as uncertain instead of being presented as fact.
- The note is concise enough for review: durable explanations and decisions stay; conversational filler and one-off tool noise are excluded.
- The index entry for every created or updated topic is current after the run.

## Methodology Guidance

Prefer topic-centered notes over session-centered notes. A single session may update several topic files, and a single topic file may accumulate knowledge across many sessions.

When deciding whether two discussions belong to the same topic, treat these as duplicate signals:

- same core concept with different names (`FTS`, `full-text search`, `Postgres text search`),
- same implementation strategy with deeper follow-up detail,
- same interview answer area,
- same project decision revisited with more nuance.

Treat these as separate topic signals:

- different layers of the stack with separate failure modes,
- concept vs implementation runbook,
- general technology explanation vs project-specific deployment decision, if combining them would make the note hard to review.

For tradeoff capture, prefer this shape:

```text
We chose A over B because <constraint>. A gives <benefit>, but costs <tradeoff>. B remains useful when <condition>.
```

For interview preparation, preserve questions that test understanding rather than memorization, such as:

- "Why use Docker for this project if it already has a Python venv?"
- "How does FTS differ from embedding vector search?"
- "Where can an agent pipeline fail, and how would you instrument it?"

## Known Pitfalls

No project-specific failures have been recorded for this skill yet. Add only pitfalls that occur in real use, such as duplicated notes, stale index entries, or summaries that confuse project progress with learning material.

# Experience Memory — Design

**Status:** draft for review (nothing built yet)
**Date:** 2026-10-31
**Author:** Jev-r (Claude) with Eduardo

## Intent

Today jev-runner's accumulated knowledge lives in two places:

- **Flows** — structured, replayable step sequences (`flows/*.json`), keyed by target + start +
  goal, executed deterministically with the money/secret guards. These work well and stay as-is.
- **`lessons.md`** — a single freeform file of human/agent-written playbooks ("avoid the Quiero
  sheet", "hide keyboard before Continuar", "SINPE needs Código BAC"). Agents read it **wholesale**.

`lessons.md` is already the thing most responsible for runs improving over time, but reading the
whole file every run doesn't scale and can't be queried. This subsystem turns accumulated
experience into a **queryable local memory**: every run leaves a scrubbed record, and a new run
retrieves the lessons/hints relevant to *this* goal and screen, injecting them into Jev's context.

This is retrieval-augmented experience. It **augments** Jev's live decisions; it does **not**
become a second, unguarded executor, and it does **not** replace structured flows.

## Goals

- Every run emits a **scrubbed** experience record (what was attempted, how it went, what helped).
- On a new run, retrieve the **top-K relevant lessons/hints** by goal + current screen and inject
  them into the decision/field-text prompts.
- Record **run metrics** (status, verify outcome, steps, duration, loop/stall events) so we can
  measure whether retrieval actually helps.
- Fully **local**. No run data, screen text, or embeddings leave the device.
- Backward compatible: `lessons.md` content is migrated in; flows are untouched.

## Non-goals / anti-goals

- **Not** semantic retrieval of executable *steps*. Steps replay via the existing structured flow
  store (exact keys + per-step screen match + guards). Retrieving "close enough" steps to execute
  is dangerous, especially for money. Memory returns *knowledge*, flows return *steps*.
- **Not** model fine-tuning / gradient learning. This is RAG over experience, not training.
- **Not** a hosted vector DB or embeddings API — both would ship sensitive data off-device.
- **Not** autonomous bank login or any change to the money/secret guards.

## Hard constraints (inherited)

- **Secrets never stored or embedded.** Ingest only consumes already-scrubbed records (reuse the
  existing scrub path). A record that still contains a secret is dropped, not stored.
- **PII care.** Screen text can contain account fragments / recipient names. Even though the store
  is local, ingest strips/limits obvious PII (digit runs, masked-account patterns) before storing
  retrievable text. (Open question below on how aggressive.)
- **Local only**, under the existing data dir (e.g. `~/.local/share/jev-runner/memory.db`).

## Architecture

```
run (Runner)  ──emit──▶  Experience store (local sqlite)
   ▲                         │
   │                         ├── lessons: retrievable knowledge (text + tags + metadata)
   │                         └── runs:    metrics (status, verify, steps, duration, events)
   │                         │
   └──inject hints◀──query───┘   (by goal + current screen, top-K)
```

- **Store:** one local sqlite file. Phase 1 uses **FTS5 (keyword)** retrieval — zero heavy deps,
  fully local, usually enough at our scale (hundreds–low-thousands of lessons). The schema leaves
  room for a **vector column (sqlite-vec) with local embeddings** added in Phase 2 if the metrics
  show keyword retrieval is missing relevant lessons.
- **Two tables:**
  - `lessons(id, created_at, target, app, goal_text, screen_tags, body, source, pii_scrubbed)`
    — the retrievable knowledge. `body` is the lesson text; `app`/`target`/`screen_tags` are
    structured filters so retrieval can scope ("android + net.bac.sbe.android" first, then rank).
  - `runs(id, ts, target, app, goal, status, reason, verified, steps, duration_ms, loop_events,
    flow_used, flow_saved)` — metrics, for measuring lift and for later analytics/route work.
- **Ingest:** at run end, the Runner writes a `runs` row (metrics) always, and a `lessons` row when
  the run produced durable knowledge (a verified success worth a hint, or a notable failure mode).
  Agents/humans can also add a lesson explicitly (replaces hand-editing `lessons.md`).

## Retrieval + injection

- **Query:** given the current goal (+ params) and the current screen (app/url + visible labels),
  return top-K lessons, scoped by `target`+`app` first, ranked by FTS match (Phase 2: + vector
  similarity). K small (e.g. 3–5) to keep the prompt tight.
- **Injection point:** fold the retrieved hints into the context the model already receives —
  `goal_text` / `field_context` / the `choose` decision context — as a short "What we've learned"
  block. This is the same surface the goal and history already use; no new execution path.
- **Guardrail:** hints are advisory text to the model, never commands the runner executes directly.
  The money/secret guards and the loop detector are unchanged and still arbitrate every action.

## Metrics (measure-first)

Recording `runs` from day one lets us answer: does a run with retrieved hints do better (higher
verified-done rate, fewer steps, fewer loop/stall events) than without? That evidence decides
whether Phase 2 (vectors) and later route-optimization are worth building — rather than guessing.

## Phasing

1. **Phase 1 (this spec):** sqlite store; `runs` metrics; `lessons` table + FTS5 retrieval; ingest
   at run end; migrate `lessons.md`; inject top-K hints into the decision context. Measurable.
2. **Phase 2 (later, gated on Phase 1 metrics):** local embeddings + sqlite-vec vector column;
   hybrid keyword+vector ranking. Only if Phase 1 shows keyword retrieval misses relevant lessons.
3. **Phase 3 (future):** use the `runs` data for route/path analytics and fallback-route finding.

## Risks / open questions

- **PII scrubbing aggressiveness** — how hard to strip screen text before storing it retrievably
  (digit runs, names). Too aggressive hurts retrieval; too little risks storing account data even
  locally. Proposed default: strip long digit runs + masked-account patterns; keep labels/verbs.
  *Needs Eduardo's call.*
- **What makes a run worth a `lessons` row** — verified successes and notable failures, but the
  exact trigger needs definition so the store doesn't fill with noise.
- **Who writes lessons** — auto-ingest vs agent/human-authored vs both (proposed: both; auto for
  metrics + a conservative auto-lesson, explicit for curated playbooks).
- **`lessons.md` migration** — one-time import; then is the file retired or kept as a human-editable
  mirror? (Proposed: import once, keep the DB as source of truth, optional export for humans.)

## Decisions needed before building

1. Phase 1 scope as written (keyword + metrics, vectors deferred) — OK?
2. PII scrubbing default (above) — OK, or stricter/looser?
3. `lessons.md`: import-and-retire, or import-and-mirror?

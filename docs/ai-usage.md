# How AI was used to build this

The project was built with Claude (Claude Code) working from the issue backlog (#1–#14),
one issue per commit.

The workflow that mattered: every ingest and tool change was verified against a **live
Neo4j** (an in-process test harness) rather than assumed — the reference numbers
(21-exercise pool, 6 exclusions, 16/16 condition reachability) were checked on the real
graph before each commit, and two real bugs were caught that way (a SKOS `narrower` mapping
making a promote rule over-fire, and a dropped rule-escalation flag).

LLM-dependent paths were validated with deterministic stand-in models so the pipeline,
validators, and streaming protocol are exercised end-to-end without an API key. The curated
ontology mappings in [`data/ontology/`](../data/ontology/) were verified against the live
NCI EVS / BioPortal APIs at curation time rather than recalled.

All AI-written code went through the same checks as any code: `pytest`, `tsc`, lint, and the
golden scenarios.

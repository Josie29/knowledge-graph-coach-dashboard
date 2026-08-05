# Agent Framework Options (2026)

Landscape scan for the agentic runtime behind the workout generator (build step 5) and the coach copilot (build step 6).

**Scope assumptions:** Python backend (FastAPI) + React, Neo4j via Docker, Anthropic models — carried over from [knowledge-graph-options.md](./knowledge-graph-options.md); 1-day build; $0 budget.

**What the spec actually needs:** typed structured output (workout plan + PROV-O trace), streaming chat with charts, ~5s responses, token efficiency. Explicitly *not* the agent's job: safety filtering — "the safety decision must be a graph traversal, not a sentence in the prompt." Nice-to-haves the framework can cover: multi-agent orchestration, streaming, observability/tracing, eval pipeline.

## Options

| Option | Description | Key pros | Key cons | Pricing |
|---|---|---|---|---|
| **Pydantic AI** | Python agent framework from the Pydantic team: typed outputs, dependency injection, OTel-native; v1 since April 2026 | Typed output *is* the API contract — hits the "typed contracts" rubric line directly; DI passes the Neo4j driver into tools cleanly; native OpenTelemetry covers the tracing bonus; AG-UI + Vercel AI SDK/AI Elements adapters stream to React without hand-rolled SSE; lightest dependency surface | Thinner orchestration than LangGraph (durable execution delegated to Temporal/DBOS); smaller ecosystem; fast release cadence | Free, open source |
| **LangGraph** | Graph-based Python/JS orchestration: durable execution, checkpointing, HIL interrupts, memory | Conventional safe pick — largest ecosystem, most reviewer familiarity; explicit state graph reads well | Boilerplate for a runtime that's mostly deterministic Python; durability/resume/interrupts are irrelevant at sub-5s single-turn; graph abstraction tempts modeling safety as agent state instead of Cypher | Free OSS. LangSmith: free 5k traces/mo, Plus $39/seat/mo, overage $2.50/1k traces |
| **Mastra** | Opinionated TypeScript framework: durable workflows, first-class memory, evals as primitives, AI SDK streaming at the UI edge | Best all-TS option — agents + workflows + memory + **evals** + tracing in one dep (two nice-to-haves free); single Next.js repo is the strongest one-command DX | Flips the ontology-ingest language — no rdflib; TS RDF tooling (N3.js, rdflib.js) is weaker for SKOS/PROV-O | Free, open source |
| **Vercel AI SDK** | Most-installed TS toolkit for streaming chat and tool-calling UIs; model-agnostic | Best-in-class streaming UX; AI Elements gives the copilot panel nearly free; production-hardened | Not an agent framework — no durable workflows, no first-class memory, no orchestration past a tool loop. Fine as UI layer, not as runtime | Free, open source |
| **Claude Agent SDK** | Claude Code as a library (Python + TS): full harness, built-in Read/Write/Bash/Grep/WebSearch, MCP-native, subagents | MCP-native if the graph is exposed as an MCP server; strong subagents; batteries included | Wrong paradigm — "give the agent a computer," built for filesystem/coding agents; built-in tools are surface area to explain away | Free SDK + Claude API tokens (Opus 5: $5/$25 per MTok) |
| **OpenAI Agents SDK** | Lightweight Python framework: agent loop, handoffs, guardrails, sessions, voice | Minimal abstraction; clean multi-agent delegation; swap LLMs freely | Handoff-centric model is overkill for two agents sharing a tool layer; ecosystem gravity pulls toward OpenAI | Free OSS + provider tokens |
| **Anthropic SDK directly** (Tool Runner) | No framework — `client.beta.messages.tool_runner` over your tools; `output_config.format` for structured output | Nothing to defend; structured outputs enforce the schema at the API layer; per-turn hooks cover approval/logging/retries; fewest moving parts | Hand-roll streaming to React, tracing, and eval scaffolding — the three nice-to-haves a framework hands you | Free SDK + tokens |
| **Google ADK** | Google's agent dev kit: model-driven loop, multi-agent, Vertex integration | Good multi-agent primitives; solid tooling | Gemini/Vertex gravity; no edge over the Python options here | Free OSS + model tokens |
| **CrewAI** | Role-based multi-agent orchestration | Fastest prototyping; demos well | Role-play multi-agent is theater here — a "Safety Officer agent" is the exact anti-pattern the rubric calls out. Weakest fit | Free OSS |
| **Strands Agents** | AWS model-driven agent loop; simplicity over control | Very little ceremony | Newer, smaller ecosystem; no typed-contract edge over Pydantic AI | Free OSS |
| **Microsoft Agent Framework** | Graph-based orchestration; successor to AutoGen + Semantic Kernel | Precise control; enterprise/.NET story | Heaviest option; Azure-oriented; nothing needed here | Free OSS |
| **LangChain DeepAgents** | Higher-level agent abstraction over LangGraph | Less boilerplate than raw LangGraph | Extra layer on an already over-provisioned stack | Free OSS |

**Observability (orthogonal):** Langfuse — MIT, framework-agnostic, OpenTelemetry-based. Free tier 50k units/mo, Core $29/mo, self-host free. Cheapest path to the tracing nice-to-have from any option above.

## Recommendation

**Pydantic AI on FastAPI, with two agents over one shared deterministic tool layer.**

```
tools/  (plain Python — no LLM, fully unit-testable)
  resolve_concepts(text) -> list[ResolvedConcept]           # exact → fuzzy → vector, explicit thresholds
  safe_exercise_pool(member_id, constraints) -> PoolResult  # Cypher traversal + provenance paths
  member_context(member_id, question) -> ContextSlice       # KG 2 retrieval

agents/
  workout_agent = Agent(output_type=WorkoutPlan)     # typed → renders directly in React
  copilot_agent = Agent(output_type=CopilotAnswer)   # streams; chart specs as a union member
```

1. **The typed output is the deliverable.** `WorkoutPlan` (warmup/main/cooldown, sets/reps/rest, per-exercise `ProvenanceTrace`) is validated before it reaches the browser and doubles as the FastAPI response model. No parsing layer, no retry-on-malformed-JSON.
2. **The LLM is structurally out of the safety path.** `safe_exercise_pool` returns an already-filtered pool plus the justifying graph paths. "What if it hallucinates a barbell exercise?" → "it can't, the tool never returned one."
3. **DI fits the graph.** Neo4j driver, member ID, and confidence thresholds enter as typed deps — which is also what makes the resolver and safety filter testable in isolation, the two required tests in build step 8.
4. **Streaming and tracing come nearly free.** AG-UI + Vercel AI Elements stream the copilot into React; native OpenTelemetry emits LLM calls, tool calls, and instrumented graph queries into Langfuse's free tier.
5. **Two agents is honest multi-agent.** Different output types over a shared tool layer is a real boundary — better in review than role-played personas, at no extra cost.
6. **Matches the KG choice.** rdflib for ingest, Neo4j Python driver at runtime, embeddings for resolver pass 3 — one language, one dependency file, one `docker compose up && uv run`.

**Latency:** keep the loop shallow — two tool calls, then generate. `output_config={"effort": "low"}` or `"medium"` on `claude-opus-5` is the main lever (low/medium are unusually strong on this model). Cache the system prompt + ontology preamble (`cache_control: {"type": "ephemeral"}`; Opus 5's cache minimum is 512 tokens). Stream both surfaces so time-to-first-token is what the coach perceives.

**Runner-up: Mastra**, if shipping one Next.js repo. Evals and tracing are first-class, and one-command DX is unbeatable. Cost: SKOS/PROV-O ingest in TypeScript without rdflib — fine if hand-authoring the ontology subset (which the spec permits), painful if parsing real OWL.

**Rejected: LangGraph.** Its differentiators (durable execution, resume-after-failure, checkpointing, HIL interrupts) all serve long-running agents. This must answer in five seconds — boilerplate for machinery never exercised.

**Rejected: CrewAI.** A "Safety Officer" agent is precisely the failure mode the rubric names. Safety is a Cypher traversal; a persona wrapper makes it look probabilistic when its whole value is that it isn't.

**Tight-on-time fallback:** drop to the Anthropic SDK tool runner with `output_config.format`. Loses the streaming adapter and free OTel; the tool layer and typed schema are unchanged — a subtraction, not a rewrite.

**Open question:** Python backend + separate React app, or a single Next.js repo? The latter flips the pick to Mastra with a hand-authored ontology subset.

## Sources

- [The best AI agent frameworks in 2026 — LangChain](https://www.langchain.com/resources/ai-agent-frameworks) · [AI Agent Frameworks Compared: Which Ones Ship? — Chanl](https://www.channel.tel/blog/ai-agent-frameworks-compared-2026-what-ships)
- [Pydantic AI v1.87 Closes the LangGraph Gap — Groundy](https://groundy.com/articles/pydantic-ai-v1-87-closes-the-langgraph-gap-deferred-tool-calls-opentelemetry/) · [Pydantic AI vs LangGraph (2026) — Ertas AI](https://www.ertas.ai/compare/pydantic-ai-vs-langgraph)
- [AG-UI | Pydantic Docs](https://ai.pydantic.dev/ui/ag-ui/) · [Pydantic AI support for Vercel AI Elements](https://pydantic.dev/articles/pydantic-ai-ui-vercel-ai)
- [Mastra vs LangGraph vs Vercel AI SDK — Particula](https://particula.tech/blog/mastra-vs-langgraph-vs-vercel-ai-sdk-typescript-agents)
- [Claude Agents SDK vs OpenAI Agents SDK vs Google ADK — Composio](https://composio.dev/content/claude-agents-sdk-vs-openai-agents-sdk-vs-google-adk)
- [Langfuse vs LangSmith (2026) — Morph](https://www.morphllm.com/comparisons/langfuse-vs-langsmith) · [LangSmith pricing in 2026 — Coverge](https://coverge.ai/blog/langsmith-pricing)

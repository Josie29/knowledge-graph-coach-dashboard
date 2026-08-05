# Knowledge Graph Stack Options (2026)

Landscape scan for the graph store behind KG 1 (movement/clinical) and KG 2 (member context).

**Scope assumptions:** ~200 nodes (50 exercises, 19 muscle groups, 9 joints, 36 movement patterns, 32 equipment, 1 member); 1-day build; $0 budget; graded on ontology grounding, deterministic safety traversal, and PROV-O explainability; reviewer runs it locally.

**Trap:** Kùzu was acquired by Apple and its repo archived Oct 2025. Any 2024–2025 tutorial recommending Kuzu for embedded GraphRAG is stale — the live successors are LadybugDB and Bighorn.

## Options

| Option | Description | Key pros | Key cons | Pricing |
|---|---|---|---|---|
| **Neo4j** (Community / Aura Free) | Property graph, Cypher; `neosemantics` (n10s) imports RDF/OWL/RDFS/SKOS; native vector index | Cypher path *is* the provenance artifact; Browser gives free viz; n10s covers SKOS grounding; vector index handles resolver pass 3 in-store; reviewers know it | Needs a running server (Docker), not embedded; GPLv3 Community; Aura Free limits inconsistently documented; property graph ≠ RDF | Community free (GPLv3). Aura Free $0 (FAQ says 200k nodes/400k rels; product page says 50k/175k — verify in console). Aura Pro from $65/GB/mo; Business Critical $146/GB/mo |
| **rdflib + Oxigraph** | RDF triplestore + SPARQL 1.1, in-process Python | Native home for OPE/COPPER/SKOS/PROV-O; provenance is literally `prov:wasDerivedFrom`; zero infra; SPARQL property paths do the injury→`part-of`→joint walk | No built-in vector index; weakest viz; SPARQL demos worse than Cypher; smaller LLM tooling | Free (Apache-2.0 / MIT) |
| **LadybugDB** | Embedded columnar graph DB, Cypher, Python/Node/Rust — the Kuzu successor fork | True embedded, single file, no server; Cypher without Docker | Young fork, unproven maintenance; vector/FTS support undocumented; risky for a graded deliverable | Free (MIT); support quote-only |
| **FalkorDB** | Redis-based property graph tuned for GraphRAG latency; experimental Bolt support | Very fast multi-hop at P99; first-class Graphiti backend; usable free cloud tier | Source-available, not OSI open source; 100MB free ceiling; thinner tooling/viz | Free tier 100MB RAM; Startup from ~$73/mo per 1GB ($0.10/GB-hr) |
| **Memgraph** | In-memory property graph, Cypher-compatible | Fast; cheap migration path off Neo4j; Community is free OSS | In-memory persistence needs care; enterprise features sales-gated | Community free; Enterprise contact-sales |
| **ArcadeDB** | Multi-model (graph/doc/KV/vector/TS/FTS), runs embedded on the JVM | Graph + vector + FTS in one process; documented Kuzu migration; Cypher/Gremlin/SQL | JVM-centric — awkward for Python/TS stacks; small community | Free, open source |
| **Ontotext GraphDB** | Enterprise RDF store with OWL reasoning + SHACL | Real OWL inference, not simulated; SHACL validation | Heavyweight for 200 nodes; Free edition needs an emailed license key from v11 | Free edition $0 (single-core); EE per-core quote |
| **Stardog** | Enterprise KG platform: reasoning, virtualization, semantic layer | Strong reasoner; mature semantic-layer story | Overkill and expensive here; sales-gated | Free 1-yr renewable license; AWS AMI ~$5.55/hr (~$4k/mo software alone) |
| **Graphiti** (Zep) | Apache-2.0 temporal KG library for agent memory; Neo4j/FalkorDB/Neptune drivers (Kuzu driver deprecated) | Bi-temporal edges fit KG 2 (adherence, churn, "what was true when"); hybrid retrieval built in | Opinionated extracted schema fights a hand-authored KG 1; still needs a backing DB; LLM ingest cost | OSS free. Zep Cloud: 10k credits/mo free; Flex $25/mo; paid from ~$125/mo |
| **LightRAG** | Stripped-down GraphRAG: simple extraction, flat graph, graph+vector retrieval | ~70–90% of MS GraphRAG quality at ~1/100th indexing cost | Built for text corpora, not curated ontologies; extracted graphs aren't auditable enough for safety | Free OSS + LLM tokens |
| **Microsoft GraphRAG** | Corpus → entity graph + community summaries → multi-hop query | Best-in-class global/thematic reasoning over large corpora | Indexing cost notorious ($33k for a large corpus in 2024); wrong shape for 50 structured exercises | Free OSS + heavy LLM tokens |
| **cognee** | Modular memory engine; composable DAGs over pluggable graph + vector stores | Store-agnostic (Neo4j, FalkorDB, …); easy backend swaps | Extra abstraction to learn and defend in a 1-day build | Free, open source |
| **NetworkX / rustworkx** | In-memory graph libraries, no persistence | Zero infra; 200 nodes fit trivially; full traversal control | No query language, persistence, or viz — reads as "skipped the graph store" | Free |
| **DuckPGQ** (DuckDB) | SQL/PGQ graph extension on DuckDB, embedded | Embedded analytical SQL + graph in one file; fast-growing in 2026 | Community extension, still maturing; SQL/PGQ unfamiliar to most reviewers | Free |

Not shortlisted: Amazon Neptune, TigerGraph, PuppyGraph, Dgraph — managed/scale-out systems with no benefit at 200 nodes.

## Recommendation

**Neo4j Community via Docker Compose at runtime, with `rdflib` as a build-time ingest tool.** Hand-author the ontology subset; don't parse full OWL.

1. **The graded artifact is the traversal, and Cypher shows it best.** Safety must come from walking `injured_joint -[:PART_OF*]-> region <-[:LOADS]- exercise`, with a provenance trace naming the justifying path. A returned Cypher path serializes straight into that trace.
2. **One store covers all three resolver passes.** Exact match on `:Concept {canonical}`, fuzzy via full-text index, embedding fallback via the native vector index — no second system, no sync problem, thresholds in one place.
3. **Two bonus items come free.** Graph visualization via Neo4j Browser (screenshot in the README); query logging gives the graph half of the observability bonus.
4. **rdflib at build time, not runtime.** Parse the OPE/COPPER/SKOS/PROV-O subsets offline in `scripts/build_kg.py`, emit CSV/Cypher, load once. Real ontology IRIs and defensible `skos:exactMatch`/`skos:broader` edges with no reasoner in the request path. Use n10s instead only if importing whole SKOS vocabularies rather than curating.
5. **Zero cost and risk.** Community edition, local Docker, no account, no free-tier ceiling to trip mid-review.

**Runner-up: `rdflib` + Oxigraph in-process.** Pick this if the ontology grounding should be literal — SKOS/PROV-O as actual triples, SPARQL property paths for the safety walk, no Docker between reviewer and app. Trade: no Browser screenshot, and SPARQL reads worse when defending the design aloud. Flips to first place if the review environment can't be assumed to have Docker.

**Rejected: Graphiti**, despite the temporal fit for KG 2. Its schema is extracted and opinionated; KG 1 must be hand-authored and auditable because a wrong edge is a safety failure. Model member-context validity as `valid_from`/`valid_to` properties on Neo4j edges instead.

**Rejected: LightRAG / Microsoft GraphRAG.** They construct graphs from unstructured text; the inputs here are two clean JSON files plus published ontologies. Using them would be the exact failure mode the rubric names — "semantic search with extra steps."

**Open question:** hosted deploy (Vercel/Railway) vs. local-only repo. Hosted pushes toward Aura Free or FalkorDB's free tier for the managed endpoint — check Aura Free's actual node limit in the console first.

## Sources

- [ArcadeDB: Neo4j Alternatives in 2026](https://arcadedb.com/blog/neo4j-alternatives-in-2026-a-fair-look-at-the-open-source-options/)
- [The Register: KuzuDB graph database abandoned](https://www.theregister.com/software/2025/10/14/kuzudb-graph-database-abandoned-community-mulls-options/1142229)
- [LadybugDB](https://ladybugdb.com/) · [The Data Quarry: From Kuzu to Ladybug](https://thedataquarry.com/blog/from-kuzu-to-ladybug/) · [gdotv: Kuzu forks, DuckDB goes graph](https://gdotv.com/blog/weekly-edge-kuzu-forks-duckdb-graph-cypher-24-october-2025/)
- [Neo4j AuraDB FAQ](https://neo4j.com/cloud/platform/aura-graph-database/faq/) · [Modern DataTools: Neo4j Pricing 2026](https://www.modern-datatools.com/tools/neo4j/pricing) · [neosemantics (n10s)](https://neo4j.com/labs/neosemantics/)
- [FalkorDB Free Tier](https://docs.falkordb.com/cloud/free-tier.html) · [FalkorDB Plans](https://www.falkordb.com/plans/)
- [getzep/graphiti](https://github.com/getzep/graphiti) · [Zep Pricing](https://www.getzep.com/pricing/)
- [Graph RAG in 2026: What Works in Production](https://www.paperclipped.de/en/blog/graph-rag-production/)
- [Trainmarks: Benchmarking 11 RDF Frameworks](https://veronahe.substack.com/p/trainmarks-benchmarking-11-rdf-frameworks)
- [Ontotext GraphDB](https://www.ontotext.com/products/graphdb/) · [Stardog Pricing](https://www.stardog.com/pricing/)

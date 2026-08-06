# `data/ontology/`

Hand-curated ontology mappings and safety rules for KG 1, the movement/clinical graph.

**Data only — no code.** These files are consumed by an `rdflib`-based build script that expands the
prefixes, emits SKOS and PROV-O triples, and loads Neo4j.

The reasoning behind every file — node and edge semantics, what was pulled from each published
ontology and what was deliberately left out — is in **[`docs/kg1-schema.md`](../../docs/kg1-schema.md)**.

## Files

| File | Contents |
|---|---|
| `namespaces.json` | Prefix registry. Every `target` elsewhere resolves through this; no other file hardcodes an IRI. |
| `joints.json` | 9 catalog joints → SNOMED CT, with laterality variants and region parents. |
| `muscle-groups.json` | 19 catalog muscle groups → SNOMED CT (primary) and NCIT via OPE (secondary). |
| `movement-patterns.json` | 36 catalog patterns, normalised into a faceted taxonomy, each carrying the 11 biomechanical attributes the safety rules match on. |
| `equipment.json` | 32 catalog equipment terms, classified, with a directed substitution graph. |
| `anatomy-partonomy.json` | 173 SNOMED body structures and 158 hand-classified `PART_OF` / `IS_A` edges. Defines the safety closure. |
| `conditions.json` | 16 injury/condition concepts → SNOMED CT, each anchored to a body structure. |
| `contraindications.json` | 22 rules mapping conditions to unsafe — and explicitly safe — movement. |
| `prov-model.ttl` | PROV-O application profile: 14 classes for recording why a recommendation was made. |
| `prov-example.ttl` | A worked provenance trace for one real decision, using real catalog UUIDs. |

## Conventions

- **Predicates** are SKOS: `exactMatch`, `closeMatch`, `broader`, `narrower`, `related`. Meanings are
  defined in `namespaces.json`.
- **Confidence** is `high` / `medium` / `low`. A `low` mapping is display-and-traversal only and is
  never the sole basis for a safety decision.
- **Declined mappings are recorded, not omitted.** Where no published concept fits, the concept
  carries a `declined` block naming the vocabularies checked and why each failed. A term is minted in
  the `kgc:` namespace only after that analysis.
- **Every SNOMED code was fetched live** from the NCI EVS REST API (`snomedct_us` 2025_09_01); every
  OPE and COPPER IRI from the BioPortal API. Nothing here was written from memory. Re-runnable
  commands are in `docs/kg1-schema.md` §10.

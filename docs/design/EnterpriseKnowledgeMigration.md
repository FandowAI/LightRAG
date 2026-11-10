# Enterprise knowledge and identity migration

`ENTERPRISE_KNOWLEDGE_ENABLED=true` opts the server's existing Neo4j/Milvus
merge manager into scope-aware similarity decisions. Generic callers retain
the legacy behavior. This migration contains extraction policy, source-owned
facts, a verified glossary, and automatic similarity-based resolution. It
introduces no review queue and never quarantines or drops an uncertain object's
knowledge.

## Extraction and facts

The versioned `prompts/entity_type/fando_enterprise.yml` profile derives from
`prompts/enterprise/knowledge_guidance.txt`. Set `PROMPT_DIR` to this checkout's
`prompts` directory and `ENTITY_TYPE_PROMPT_FILE=fando_enterprise.yml`. Existing
published WebUI overrides retain precedence and must be checked before rollout.
The profile organizes objects into twenty business types while retaining facts,
methods, constraints, competing opinions, historical context, and explicit
decision grounds. It does not invent private reasoning or treat a plan as an
implemented rule.

JSON extraction accepts `knowledge_records`, `identity_scope`, and `aliases`.
`enterprise_knowledge` stores complete records as JSON in the graph, with chunk
IDs assigned by the parser, separately from aggregate descriptions. The existing
token-budgeted query context includes those records. Storage retention is not
limited by the summary fragment cap. Query token limits can still omit records
or entities; source chunks remain the authoritative original evidence.

Observed aliases and facts are owned by source chunks. Deletion prunes them
against the full authoritative tracking list, not the truncated graph source-ID
view. Cached extraction fragments replay committed, source-scoped identity
mappings when rebuilding nodes and relation endpoints; purge never asks a model
to make a new identity decision. Existing anchor and custom-chunk journal ordering
is unchanged: identity resolution precedes the first graph mutation and its
recovery records. Cancellation observed after name resolution prevents graph
mutation.

## Roster and identity

`ENTITY_IDENTITY_REGISTRY_FILE` points to a private JSON roster outside Git.
`scripts/compile_enterprise_registry.py` combines the existing compiled glossary
with optional reviewed aliases. Primary canonical names win. Source-specific
corrections stay source-specific; pilot hash-suffixed display names are excluded.
An entry needs an explicitly verified marker and evidence. Ambiguous aliases do
not become facts. A `*` type permits an approved spelling alias without changing
the object's extracted type. Neither a person alias nor the current roster
changes a historic job title or role.

The optional roster `context_scope` limits unscoped aliases to the company corpus.
An explicit conflicting company prevents a roster match; an entry's reviewed
scope overrides this default. This boundary never asserts employment or writes
an otherwise unknown company into an entity's identity fields.

The server first checks verified aliases. Other candidates come from the existing
typed name VDB and an indexed, workspace-local SQLite alias table. It never scans
all graph nodes. Explicit scope conflicts cannot merge. Matching aliases and
exact names have score 1.0; other scores come from the name-vector cosine search.
After type and scope checks, candidates are ordered by descending score and name.
The best candidate is selected when its score reaches `similarity_threshold` and
its lead over the runner-up reaches `margin_threshold`. A runner-up below the
similarity threshold still participates in the margin check. With one candidate,
only the similarity threshold applies. Both thresholds are existing workspace
settings; this change does not replace them with new defaults.

Similarity scores are heuristic selection criteria, not proof that two objects
share an identity. Distinct objects with matching types and compatible or missing
scope can now merge when the score gates pass. Source-owned facts and alias
provenance are retained; existing graph decisions are not automatically revisited.

Name resolution no longer calls the extract-role model, reads original chunks
for identity verification, or performs a second model check. Document decision
metadata records the best and runner-up scores; `judgment` remains null. Threshold
or margin failures preserve separate objects and their source-owned content.
Same-name conflicts receive a deterministic suffix derived from type, scope,
and source identity. The settings API reports model identity verification as
disabled and `method=vector_similarity`, while retaining the roster snapshot.

The existing per-workspace merge lock spans decisions, anchor/journal writes,
graph mutation, and name/alias-index publication. This supports the server's
existing concurrent document ingestion rather than requiring a serial local
pilot. Extract-role limits still apply to extraction and other existing model
calls; identity resolution no longer consumes that request pool. Turning
automatic similarity resolution off leaves explicitly verified roster aliases
active. Preview returns the name-only score decision without model calls; actual
ingestion additionally applies source-specific roster and scope rules.

## Recovery and accepted residue

The alias table is a candidate index, not authoritative identity data. It is
updated only after graph writes; every candidate is rechecked against the graph.
Manual rename/delete, document purge, and clear retain the existing lifecycle
hooks. A successful clear also removes workspace alias rows. As with the existing
name VDB, a crash can leave stale index rows or omit a newly committed alias.
Stale rows cannot authorize a merge: the node must still exist with the right
type and compatible scope. An omitted alias can cause a separate object
until reprocessing or an explicit index refresh. This is an accepted conservative
residue; there is no cross-database transaction and no automatic historical merge.

Private roster JSON, credentials, input documents, and live indexes are not
published with the source. Rollout preserves the workspace, database endpoints,
authentication, role models, and existing Markdown outputs. New rules apply to
future ingestion; existing graph content is not automatically rewritten.

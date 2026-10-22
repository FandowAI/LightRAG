# Batch evidence retrieval

The original WebUI **批量检索** page contains selection, explicit start,
progress and distillation handoff. The ordinary retrieval tab retains normal
query settings, chat and testing; the prompt admin tab is also preserved.

## Retrieval integration

The colleague's `feat/add_graph_query_mode` commit
`69fd530d43f76c21b78b1b60c4f041339e948906` is merged, preserving graph-mode
queries, `/query/data/batch`, DFS traversal, tracking-row precedence, depth
0–5 and its 100-node/200-relation caps.

`entity_packages.from_rag` adapts the shared `_traverse_graph_from_entity`
implementation rather than scanning every graph node for each center. It adds
vector candidates (default 30), processed-document gates, grounded topic
selection and batch-wide text deduplication. The initial center is an exact
entity name; no question-generation step is needed. Default depth is 2.
Batch jobs explicitly pass separately configurable safety caps (10,000 nodes /
20,000 relations by default; `LIGHTRAG_BATCH_MAX_GRAPH_NODES` and
`LIGHTRAG_BATCH_MAX_GRAPH_RELATIONS`) into that same traversal helper.
A capped graph is rejected for training packaging rather than silently
exported as a complete neighborhood. Interactive graph queries retain the
colleague's existing truncation metadata behavior.

The colleague's ordered branch-barrier dedup remains available in the original
batch API. Production evidence jobs independently retrieve each center so shared
passages can be assigned to the most relevant center before packaging, rather
than deleted according to request order. Graph edges remain contextual indexes;
only source text selected for a center enters its teacher text. See
[FocusedEvidence.md](FocusedEvidence.md) for relevance and literal-span rules.

Core selection currently supports at most 10,000 nodes and reads the graph and
tracking anchors once per selection request. It is not an unbounded graph-wide
analytics service. The browser exposes only neighbor/document thresholds,
then depth/vector count, followed by actual phase progress and package counts.

## Durable jobs and files

A workspace-local SQLite ledger allows one active batch. Retrieval runs against
the server's initialized RAG instance without creating another graph writer.
The selector uses four bounded workers and the existing role wrapper; it never
raises the shared extraction-role limit or changes operator model settings.
Failed/uncertain selection is retained for review, not copied into teacher data.

`LIGHTRAG_BATCH_OUTPUT_DIR` is an optional persistent root. A job lives at
`<root>/<workspace-or-default>/<batch_id>/`. Without it, the compatible fallback
is `<working_dir>/<workspace>/batch_retrieval/<batch_id>/`.
The H200 template configures `/data/fando/lightrag/exports/enterprise/`.

Each terminal successful batch publishes `packages.jsonl`, `sources.jsonl` and
`manifest.json`. The API returns the absolute `export_path`; the UI displays it.
Each package contains entities, relationships, literal original passages,
provenance, shared-evidence references and SourceRecord-compatible text/metadata.
Full original chunks are stored once in the source archive. Manifest row counts
and hashes must be checked by consumers before import. See
[H200 deployment](../../deploy/h200/README.md) for paths, schema and rollout.

Per-center/audit JSON remains on disk. Files are atomically replaced before
completion is reported. A crash may leave an uncounted file or incomplete export;
only a manifest-validated export is deliverable. Dead PID plus process-birth
checks mark jobs interrupted. No restart automatically repeats paid retrieval.
Explicit resume requires zero published packages; packaging-only rebuild creates
a new job and preserves old outputs. Old pilot jobs without source snapshots
must be rerun before creating production source-verified exports.

## Authenticated API

- `GET /batch-retrieval/config`: adapter ID and handoff availability; no secrets.
- `POST /batch-retrieval/jobs`: centers, graph depth, vector count; returns 202.
- `GET /batch-retrieval/jobs/{id}`: real phase/progress/outcomes/export path.
- `POST /batch-retrieval/jobs/{id}/stop`: stops after in-flight calls settle.
- `POST /batch-retrieval/jobs/{id}/resume`: explicit preparation recovery.
- `POST /batch-retrieval/jobs/{id}/rebuild`: new job from a frozen snapshot.
- `GET /batch-retrieval/jobs/{id}/packages.jsonl`: terminal package records.
- `GET /batch-retrieval/jobs/{id}/sources.jsonl`: shared full original chunks.
- `GET /batch-retrieval/jobs/{id}/manifest.json`: delivery hashes and row counts.
- `POST /batch-retrieval/jobs/{id}/distill`: explicit acknowledged handoff.

All routes reuse current API authentication and workspace isolation; workspaces
are shared resources, not a new per-user access-control system. Active export
requests return 409. Errors expose types, not private provider response bodies.

Handoff is disabled until `DISTILLATION_INGEST_URL` is set. Optional Bearer key
is server-side only. The receiver must accept `{batch_id, records}`, honor
`Idempotency-Key: lightrag-<batch_id>`, and return `accepted:true`. A lost receipt
is unconfirmed; explicit retry uses the same key. The HTTP adapter does not
transfer `sources.jsonl`; shared storage or an agreed file-transfer channel is
required. Keep it unconfigured until that integration contract is confirmed.

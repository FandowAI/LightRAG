# Batch evidence retrieval

The original **批量检索** page contains center selection, explicit retrieval,
progress, package counts and handoff. Ordinary query testing and prompt
administration are preserved.

## Current flow

1. Select core entities using neighbor/document thresholds.
2. Retrieve the center, graph-path evidence and vector candidates.
3. Deduplicate within each package and across this batch.
4. Save entities, relationships and original text to the JSONL delivery files.

There is no topic/relevance model, source-review queue, or review gate. Every
successfully retrieved original enters deduplication and packaging, including
unclear transcripts and mixed business topics. Originals are never rewritten.
Only confirmed duplicate evidence is removed from teacher text. The original
variants and provenance remain in the audit/source archive.

Depth defaults to 2; ordinary vector candidates now default to 10 (previously
30) and remain editable. This value bounds VECTOR candidates, not the total
number of graph-source chunks. Graph evidence is retrieved independently; a
complete recalled block is never silently cut into selected topics. Neighbor
entities contribute relationship/path evidence, not their entire unrelated
lifetime document collection.

## Retrieval implementation

The colleague's `feat/add_graph_query_mode` commit
`69fd530d43f76c21b78b1b60c4f041339e948906` remains merged. The evidence adapter
reuses its `_traverse_graph_from_entity` DFS. Interactive query/data/batch and
its branch-barrier options retain their behavior and 100-node/200-relation
default caps. Evidence jobs explicitly pass their own configurable safety caps
(default 10,000 nodes / 20,000 relations). A capped evidence traversal raises;
it is never passed off as a complete package. No second graph writer or
per-center full-graph scan is created. Only processed source documents are used.

## Deduplication and model cost

- Identical source IDs/text and whitespace-normalized copies use local rules.
- Near candidates must meet 0.995 lexical similarity, plus matching date,
  number/unit, negation and condition guards, before any model confirmation.
- One confirmation function is shared by retrieval and cross-package dedup for
  the WHOLE batch. `LIGHTRAG_DEDUP_MAX_LLM_CHECKS` defaults to 2; zero disables
  model confirmations. The budget cannot be reset per center or package.
- A successful confirmation requires full information equivalence and exact
  supporting quotes. Merely related text cannot be merged.
- Failure, uncertainty or an exhausted budget keeps both texts and continues.
  No source waits for human review. `dedup_llm_calls` reports actual model calls.

The first successfully committed package owns shared text. Later centers keep
references; exports/handoff never re-expand repeated text. Failed/uncommitted
packages cannot suppress later evidence. Fully repeated centers are skipped as
duplicates rather than failed, with related centers linked from their owner.
The assignment is deterministic for the selected order, not an AI relevance
judgment. Source differences in time, numbers, conditions and conclusions stay.

## Durability, export and API

Jobs use a workspace-local SQLite ledger and the initialized server RAG instance.
One active batch is allowed per workspace. The UI shows real per-center retrieval
and dedup/save progress; package counts increment after atomic saves. Failed
retrieval is a technical failure, not a content-review decision. Stopping retains
completed packages and snapshots; no restart automatically repeats paid calls.

The production policy is `retrieval_dedup_v2`. Historical focused snapshots are
retained for audit, but the normal server refuses resuming their removed source
selection stage. Start a new batch to use current behavior; old files are not
silently rewritten.

`LIGHTRAG_BATCH_OUTPUT_DIR` sets the persistent output root. The H200 template
uses `/data/fando/lightrag/exports/enterprise/<batch_id>/` containing:

- `packages.jsonl`: one record per nonempty unique package, with entities,
  relationships, original text, sources and duplicate ownership references.
- `sources.jsonl`: full original chunks, stored once by ID across the batch.
- `manifest.json`: format version, file row counts and SHA-256 checksums.
- `candidate-NNNN.json` and `entity-NNNN.json`: recalled originals and dedup audit.

The API returns an absolute `export_path`, shown on the page. Original existence,
relationship endpoints and literal offsets are checked before publication.
Graph descriptions are contextual indexes, not substitutes for source facts.
See [H200 deployment](../../deploy/h200/README.md) for deployment and schema.

Authenticated routes remain under `/batch-retrieval`: `config`, `jobs`,
`jobs/{id}`, `jobs/{id}/stop`, the three artifact filenames, and `jobs/{id}/distill`.
Active or unpublished artifact requests return 409. Batches are workspace-shared
resources using the existing auth boundary, not a separate per-user ACL.

Handoff stays disabled until `DISTILLATION_INGEST_URL` is configured. The receiver
must honor `Idempotency-Key: lightrag-<batch_id>` and explicitly acknowledge
`accepted:true`. The HTTP adapter sends `{batch_id, records}`; the source archive
requires agreed shared storage/file transfer. No acknowledgement is simulated.

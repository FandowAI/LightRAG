# Core entity candidates

The original WebUI has a top-level **批量检索** page. Core-center selection is
its first step; retrieval and evidence packaging stay on that same page.
It calls authenticated `POST /query/entity-centers`. This endpoint never calls
an LLM or embedding model, modifies a graph, enqueues documents, or starts
distillation. It is independent of the embedding SSH tunnel.

## Evidence and structural scope

Only source chunks from `processed` documents contribute. Complete
`entity_chunks` and `relation_chunks` anchors outrank graph `source_id`,
including an explicitly empty anchor. Source IDs are the fallback only when
the tracking row is absent. Source documents are counted by distinct document
IDs across the entity and incident supported relationships, not by chunk count.
The document count covers graph-linked completed evidence, not all uploaded
files. Multiple edges, reverse edges and self loops cannot inflate distinct
neighbor counts. Unsupported edges do not contribute structure.

The structural-core indicator repeatedly peels nodes with fewer than two
remaining neighbors. It is a reason, not an admission requirement: an important
star-shaped hub may have no cyclic core. Screen coordinates and visual layout
do not participate in selection.

## Default, editable recommendation rules

Defaults: at least three distinct neighbors, at least two source documents,
and a recognized business type from the existing enterprise extraction taxonomy.
All business domains in that taxonomy are eligible; this is not a department
allowlist. Numeric type prefixes are normalized. Taxonomy is a transparent
proxy, never a machine determination of real business value or factual accuracy.

Rows are placed into four reviewable tiers:

- `recommended`: meets both thresholds, known business type, no review override.
- `review`: sparse departments/brands/methods/decisions/mechanisms/strategies,
  a row meeting only part of the evidence/structure test, unknown rich types,
  explicit identity uncertainty, or a broad company hub.
- `background`: supported peripheral objects with insufficient independent
  evidence/structure. They remain available as retrieval context.
- `unavailable`: no completed supporting evidence. These rows are counted but
  omitted from the interactive candidate table.

A company-type node with at least twenty neighbors and five source documents
goes to review despite meeting the ordinary thresholds. This initial heuristic
is surfaced as a caution; it is not a general relevance or overlap detector.
Important sparse knowledge remains available through the API. Unknown-type rows are
never automatically recommended merely for being highly connected.

Sort by tier, then descending source-document count, neighbor count, structural
core membership and finally canonical name. There is no opaque numerical
business-value score. The response includes the actual applied thresholds,
counts and machine-readable reason/caution codes. A graph above 10,000 nodes
fails explicitly instead of silently returning a partial ranking.

## Review and packaging workflow

The user explicitly generates candidates using two thresholds: minimum linked
entities and minimum source documents. Only recommended rows appear in the
minimal UI and start checked. The user can uncheck individual recommendations
or use the single select-all checkbox. There is no search, scope filter, export
or per-row navigation. The API still exposes all tiers for future integrations.

`批量设为中心并进入检索` freezes the selection and reveals retrieval settings
on the same page. It does not start retrieval. The explicit start button creates
a durable batch. A fresh scan replaces the candidate list and its selection.
Identity changes reset the page; active batch restoration is documented in
[BatchRetrieval.md](BatchRetrieval.md).

Packaging does not enforce candidate tiers as a hard API allowlist. Every
chosen center forms one complete neighborhood package. Peripheral nodes are
never deleted or filtered out of that package by the selection tier.

This is a live read, not a transactional snapshot. For repeatable reports use
an idle pipeline and verify graph/document/chunk files remain unchanged.
Business validation of recommendations and cross-package overlap reduction
remain separate from these rules; tests establish software behavior, not the
optimality of the default thresholds.

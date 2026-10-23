# Entity evidence packages

One selected center produces at most one logical package. See
[BatchRetrieval.md](BatchRetrieval.md) for adapter integration and
[H200 deployment](../../deploy/h200/README.md) for the published JSONL schema.

Production retrieval reuses the colleague's bounded graph-mode DFS, with
exact canonical names, depth 2 and 10 vector candidates by default. Both the
standalone endpoint and production batch return original evidence without any
topic/relevance judgment. Batches additionally deduplicate text across centers.

Tracking anchors outrank graph source IDs, including authoritative empty
tracking rows. Only processed source documents may contribute training evidence.
No whole-graph scan is performed for each package. Hitting the batch graph
safety cap rejects a training package with a clear error. Batch caps default
to 10,000 nodes / 20,000 relations and are configured separately from the
colleague's unchanged 100-node/200-relation interactive defaults.

Entity/relationship descriptions are contextual graph indexes. Relationship
endpoints and source IDs are exported explicitly, and must resolve to complete
original chunks in the shared `sources.jsonl`. The teacher text only includes
relevance-selected literal passages; no generated summary replaces its sources.

Near matching is conservative: 0.995 lexical similarity and matching guards
are required before a rare model check. The whole batch shares a default budget
of two confirmations. Exact text uses local rules. Unique, uncertain and failed
judgments keep both originals, without review. Shared text belongs to the first
successfully committed center, and later centers retain references.

A 12,000-token notice is a review hint, not a truncation or model context limit.
A logical package may require several downstream teacher requests. The current
service does not generate question-answer pairs or invent reasoning traces.

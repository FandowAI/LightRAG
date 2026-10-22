# Entity evidence packages

One selected center produces at most one logical package. See
[BatchRetrieval.md](BatchRetrieval.md) for adapter integration and
[H200 deployment](../../deploy/h200/README.md) for the published JSONL schema.

Production retrieval reuses the colleague's bounded graph-mode DFS, with
exact canonical names, depth 2 and 30 vector candidates by default. The
standalone `/query/entity-packages` endpoint returns the evidence candidate
set for one center; the batch workflow additionally selects grounded topics
and deduplicates original text across centers before publication.

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

Near matching is conservative: scope, date, numeric, unit and negation guards
must agree, then a role-model judgment must confirm full information equivalence
using exact source quotes. Similarity alone never deletes unique facts. Failed
judgments keep both originals and leave an audit. Cross-package ownership is
based on highest relevance with deterministic ties, not retrieval order.

A 12,000-token notice is a review hint, not a truncation or model context limit.
A logical package may require several downstream teacher requests. The current
service does not generate question-answer pairs or invent reasoning traces.

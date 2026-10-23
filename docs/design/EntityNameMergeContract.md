# Entity name resolution during graph ingestion

This optional overlay is active for the server's Neo4j + Milvus configuration. It does not change the ordinary entity vector collection or query behavior. Settings are stored per workspace in `entity_merge_settings.sqlite3` under `WORKING_DIR`; the separate Milvus collection is `<workspace>_name_entities_<embedding-model>_<dimension>d`.

When `ENTERPRISE_KNOWLEDGE_ENABLED=true`, the evidence-based enterprise overlay
replaces the score-based decision below. See
[EnterpriseKnowledgeMigration.md](EnterpriseKnowledgeMigration.md) for verified
glossary rules, original-source checks, uncertainty retention, and alias lifecycle.
The locking, journal, and name-index publication contracts still apply.

## Legacy decision (enterprise overlay disabled)

For each extracted entity name, look only at the committed name collection from earlier merge operations. Names extracted in the same batch are not inserted into that collection until the batch graph merge has completed. The name vector is generated from the name alone; the existing `entities` collection still embeds the name plus description. Only candidates with the same normalized entity type enter the top-K ranking. The best candidate is selected when cosine score is at least `similarity_threshold` and the difference to the second same-type candidate is at least `margin_threshold`. A single candidate needs no margin. Exact names continue through the existing graph identity path. Missing or conflicting extracted types bypass semantic resolution.

Mapping happens before the document's `full_entities` / `full_relations` write-ahead anchors, or before the custom-chunk operation journal. Both node keys and relationship endpoints are rewritten. Parallel merge operations use a workspace lock across resolution, anchor creation, graph mutation, and name-index publication. The existing merge code deduplicates relation endpoint pairs.

## Persistence and recovery

The name collection is flushed after successful graph writes and before the document is marked `PROCESSED`. If this flush fails, ingestion fails loudly; the document remains eligible for retry. The name collection and Neo4j have no cross-store transaction. An interrupted write can leave an indexed name that a later purge removes from the graph; candidate selection always verifies that the graph node still exists and has the indexed type, so such an entry cannot authorize a wrong merge. The document purge and manual entity edit/delete paths remove or refresh name entries. Clearing documents drops the collection but leaves settings intact.

One residue remains: a stale name entry may occupy a top-K search slot before the graph existence check. It can temporarily prevent an otherwise eligible merge, in which case a new node is retained rather than unrelated nodes being combined. A full clear or reindex removes that residue. Because this workspace was cleared before the feature was added, no historical backfill is required for this deployment.

Settings are read at the start of each resolution and take effect on future documents without restart. Changing thresholds does not retroactively split or combine graph nodes. The UI preview is read-only and uses the saved settings.

Processed document metadata includes counts for `merged`, `exact_name`, `no_same_type_candidate`, `below_threshold`, `ambiguous_margin`, and `invalid_type`. These counters describe the extracted names in that document; they are not counts of graph nodes or relation rows.

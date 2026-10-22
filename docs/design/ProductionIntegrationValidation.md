# Production integration validation — 2026-10-22

## Branches and integration choices

Destination: `FandowAI/LightRAG`, `feat/enterprise-knowledge-migration`, starting
at `46200dc3efc6b271065b5df4cee937899980d2c0`.

Merged colleague retrieval branch `feat/add_graph_query_mode`, commit
`69fd530d43f76c21b78b1b60c4f041339e948906`, preserving its history, graph mode,
raw-data endpoints, deterministic DFS and batch branch-barrier options.

The new evidence adapter reuses that DFS. Interactive defaults remain 100 nodes /
200 relations. Evidence jobs pass separately configurable caps (10,000 / 20,000
by default), filter to processed source documents, add vector candidates and
run relevance selection before batch-wide original-text deduplication.

The destination branch's source-owned enterprise knowledge and identity manager
remain the only production merge implementation. The local pilot's second
resolution decorator is not layered over it. Private rosters are supplied via
a server-side file, not committed to Git. Existing prompt administration and
normal query testing remain available.

The local folder uploader depended on a Windows loopback proxy. It now calls
the original authenticated upload API with a validated relative path. Filenames
are collision-resistant, parser hints survive, and a workspace-local SQLite
map retains the original folder path. Content duplicates are skipped, not failed;
actual filename conflicts still require explicit repair.

## Reproducible checks

- Backend regression: **1,706 passed**, **62 deselected**, covering pipeline,
  API routes, enterprise migration, graph mode, evidence packages, batch jobs,
  deduplication, export schema and repository docstring rules.
- Of the deselections, 54 lack the `offline` marker; eight require Linux-specific
  filesystem behavior or Windows URI expectations in existing tests. These are
  named explicitly below; they were not silently removed from the repository.
- After the last cap-configuration change, **35 focused tests passed**, including
  a regression proving batch caps do not change interactive graph defaults.
- Frontend: **671 passed**, zero failures; TypeScript check passed; lint has
  zero errors and one existing ThemeProvider fast-refresh warning; production
  build passed. The new export-path test was mutation-checked: hiding the path
  made it fail, then restoring the component made it pass.
- Python lint and formatting checked for new modules; staged whitespace and
  targeted credential-pattern checks passed. Raw documents, local private model
  catalogs, credentials, caches and live graph/index data are excluded.

Backend command (from the repository root, using a prepared Python environment):

```bash
python -m pytest tests/pipeline tests/api/routes \
  tests/extraction/test_batch_dedup.py tests/extraction/test_batch_retrieval.py \
  tests/extraction/test_entity_centers.py tests/extraction/test_entity_packages.py \
  tests/extraction/test_evidence_export.py tests/extraction/test_focused_evidence.py \
  tests/extraction/test_enterprise_migration.py tests/test_graph_query_mode.py \
  tests/test_entity_merge.py tests/test_docstring_budget.py -m offline -q
```

On this Windows host the command additionally deselected:

- `test_parse_mineru_to_lightrag_document`: existing local-path versus file-URI assertion.
- `test_upload_file_opener_creates_new_file_with_private_permissions` and
  `test_upload_file_opener_falls_back_when_dir_fd_unsupported`: POSIX mode 0600.
- `test_scan_refuses_an_unsafe_basename_and_still_ingests_its_neighbour`,
  `test_the_refused_file_stays_in_the_input_directory`,
  `test_the_refusal_happens_before_any_identity_resolution`,
  `test_the_scan_warning_names_the_offending_code_point`, and
  `test_pipeline_enqueue_file_refuses_an_unsafe_basename_without_a_doc_row`:
  Windows cannot create the control-character filenames their Linux fixtures use.

An attempted full offline suite stopped at collection because this local trial
venv lacks optional storage/provider libraries (including neo4j, grpc/pymilvus,
ollama) and Windows lacks `fcntl`. This is not reported as a full-suite pass.
The existing Linux backend and frontend workflows now also run for pushes to
the destination feature branch, with the repository's frozen dependency locks.
CI results must be checked after push; this document does not claim remote CI
has already passed.

## Delivery boundary

See [H200 deployment](../../deploy/h200/README.md) for installation, systemd,
configuration, authentication and the evidence schema.

With the supplied template the JSONL folder is:

`/data/fando/lightrag/exports/enterprise/<batch_id>/`

Deliver `packages.jsonl`, `sources.jsonl`, and `manifest.json` together. The
manifest hashes and row counts, relationship endpoints, source existence and
literal offsets are checked before a batch is marked published. The HTTP
artifact endpoints serve those same persisted files.

No H200 deployment, live production database migration, full-corpus rerun, or
real distillation-system handoff is performed as part of this code integration.
The handoff button stays disabled until its receiver/storage contract is configured.

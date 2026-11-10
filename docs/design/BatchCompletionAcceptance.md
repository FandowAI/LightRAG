# Batch workbench acceptance checkpoint, 2026-11-05

A later user review identified missing original-page parity. The subsequent
restoration is recorded in [BatchWorkbenchCompatibility.md](BatchWorkbenchCompatibility.md).
The expanded page substitutions described here are historical and are not the
current requested interface.

This checkpoint closed the twelve then-authorized test-service requirements in
[BatchCompletionPlan.md](BatchCompletionPlan.md). Production rollout and adoption
of the main `h200_lightrag` workspace are excluded by the user's test-only
restriction. The main 9621 service was not contacted, stopped or restarted.
The official `data-work-flow` checkout is clean at
`bbafde58194cc989d4b0420ebfbf6fe112cb60d4`.

## Serving candidate and verification

9631 serves the complete batch-only manager and workbench from
`config/validation/test9631-live-resume/export-fix-release/` in the parent
workspace. Wheel SHA-256:
`69878177f4aef420ef9fe42f7185a4a2a5f4b163a340ac5fd4d4a86fd6308cc3`.
The dedicated launcher defaults to this candidate. Its read-only verification
passes, and all 279 package Python files match the installed candidate's source
inventory and bytes. The listener is `0.0.0.0:9631`; LAN entry:
`http://10.10.115.253:9631/webui/`.

Final backend scope: **529 passed**, zero failures/errors/skips, pytest exit 0,
531.11 seconds. It includes batch control, workflow integration, role metadata,
auth/health/login/entry mounting and docstring contracts. Ruff and whitespace
checks passed. The existing frontend **763-test** checkpoint, frozen install,
TypeScript, lint and staged build remain applicable: all 307 recorded frontend
files and the exact source inventory are unchanged. Lint retains its original
ThemeProvider warning. Actual Firefox acceptance supplements the rendered
component tests; password login was not exercised. No browser is required by
the deployed RAG or distillation processes.

## Requirement-by-requirement audit

All evidence filenames below are under the private parent-workspace archive
`config/validation/test9631-live-resume/`, unless another checkpoint is named.

| Required behavior | Accepted evidence and practical scope |
| --- | --- |
| Global selection, clear entry and current batch | Actual `browser-acceptance.json` and screenshots; rendered manager/workbench scope tests, accepted 763-test checkpoint |
| Batch documents and graph | Two actual ingestion tasks completed against disjoint Neo4j/Milvus namespaces; document pages rendered; HTTP bridge/full-chain tests establish upload, browsing, edits and drain contracts |
| Query, centers, retrieval and distillation | `live-acceptance-complete.json`, retained query/center/retrieval task receipts and successful official workflow receipts; both batches retrieve their own original marker and reject foreign task access |
| Independent progress, cancellation, retry and stale responses | Actual progress browser view, `live-cheap-task-cancel.json`, `live-cheap-task-retry.json`; rendered request-scope and late-download tests, including deliberate mutations, establish switch/login/close boundaries |
| Existing-data inventory and exclusive ownership | Actual source inspection/declaration, adoption observation/snapshot, binding and persistent physical owner; process-scope tests reject foreign/unknown PID ownership |
| Execute authorized test adoption and retain recovery | `adoption-completed.json`, `final-adoption-backup-verification.json`; ready journal and 17 verified backup files; retained graph and vector bindings |
| Shared provider admission | `real-provider-ledger-evidence.json` records actual cross-batch model overlap; official SDK/Serving two-interpreter tests prove FIFO concurrent/request/token limits and waiting/release behavior |
| Complete explicit index rebuild | `live-index-acceptance.json` and `live-index-final.json`: fresh four-collection Milvus target, 20 expected/written/verified rows before activation; peer lease unchanged; both text queries still pass |
| Failed configuration rollback | `live-configuration-rollback.json` retains and verifies the old real runtime after unsupported-chunker preflight; application/rollback process tests additionally prove replacement-failure restoration and uncertain-stop fences |
| Training export and provenance | Actual completed Messages exports, three samples each; verified dataset/provenance/manifest downloads; all six provenance rows match original batch/task/revision; official offline tests also cover Alpaca and ShareGPT |
| Real database/model/browser acceptance | Actual simultaneous extraction, isolated Neo4j/Milvus namespaces, official QA runs and Firefox workbench checks; `parallel-real-runtime-leases.json` records the real concurrent ready leases |
| 9631 deployment and final preservation audit | `final-live-host-audit.json`: manager-only, four registered test batches, all tasks terminal, no desired/pending configuration; current launcher receipt and `final-checks.json`; unchanged official workflow checkout |

The `parallel_leases` field in `live-acceptance-complete.json` was captured just
after a deliberate manager replacement, before workers restarted, and shows
stopped leases. It is not concurrency evidence. Use the earlier actual ready
leases and the actual provider request intervals named above. This distinction
prevents a start-of-run snapshot from being represented as a live parallel run.

The adopted legacy test workspace had no document-status/full-document/chunk KV
rows, but did have 38 graph nodes, 23 edges and existing vector collections
(38 entity, 23 relationship, 20 chunk and 38 name-index rows). All were captured
in the verified backup. The populated-document preservation contract is also
covered by the earlier actual local adoption process with existing documents
and failure states, retained in the scope/business checkpoint. No claim is made
that the populated main workspace was migrated.

## Issues found and resolved during actual execution

1. Legacy public role metadata removed safe `max_tokens` options because the
   key contained `token`. Exact known nonsecret token-limit fields are now
   preserved; credential/token values remain removed. Role/adoption tests pin
   both behaviors.
2. The dedicated test scenario template still requested string scenarios while
   its selected current parser required typed objects. Only its output section
   was aligned; immutable revisions preserve the old template and original
   business instructions. Both new test batches and the two matching old test
   batches use the aligned template. RAG settings were unchanged.
3. Training export assumed every current run used contract v6. The selected
   official checkout uses current category-catalog v5 and ordinary-topic v6.
   Export now requires the exact contract derived from the frozen catalog;
   mismatches remain rejected. The existing successful paid A workflow was
   exported again without regenerating QA.

Original failed task/export receipts and the initial operator validation-script
failures are retained. One operator call initially supplied the active revision
where the application API requires the desired revision; the request was
rejected without applying it. A later raw configuration comparison correctly
noticed revision-specific prompt paths; the final acceptance compares the
existing content-normalized configuration component contract. Both issues were
in the acceptance script, not hidden service retries.

The temporary concurrency change was restored to its original value and all
content-normalized RAG/retrieval/recipe/bridge components match. Original and
abandoned vector namespaces, revisions, failed attempts and synthetic test
fixtures remain retained. Manager/control/provider SQLite backups are archived.
The short-lived browser session file has been removed; no account or password
was changed. Original sandbox EPERM and blocked-goal records remain historical;
approved native execution supplied the actual host evidence that resolved them.

# Four-step batch retrieval workbench

This replaces the batch retrieval page layout while preserving the original
candidate policy, retrieval submission and data-work-flow execution contracts.
The other four business pages, upper-left batch selection/creation and document
chunking controls remain as in BatchWorkbenchCompatibility.md.

The route is Select entities → Batch retrieval → Check packages → Distill QA.
There is no fifth result step. A separate monitor stays mounted beside the step
content, stacking below it on smaller screens. Completed QA previews, complete
review JSONL downloads and full execution logs are inside that monitor.

## Ownership and configuration

BatchRetrievalView owns the two workflow hooks and route selection. Candidate
selection retains the recommended-only policy, explicit confirmation and
explicit start. RetrievalProgress, PackagePreview, RetrievalSettingsDialog,
DistillationStartButton and DataWorkflowProgress have separate responsibilities.
Preview modules and the workbench page load on demand.

Both configuration entries are available without a retrieval job. Retrieval
preferences are saved in browser localStorage by backend prefix, signed-in
identity and data batch; the dialog exposes the existing two candidate
thresholds, graph depth and vector candidate count. Per-run form changes remain
available. Defaults are unchanged. Distillation settings use the existing
server-side workspace overrides and prompt editor. Running retrieval and
workflow jobs retain their captured settings; a settings save affects future
jobs. No worker restart is needed for these business recipe overrides.

Changing steps does not unmount the monitor or resubmit a task. Changing the
signed-in identity or selected data batch remounts the page and cancels stale
requests through the existing application scope. Restoration references are
session-scoped by identity and batch. Reselecting entities is unavailable during
retrieval or active distillation. A remembered workflow can remain visible after
selection is reset; it cannot be submitted as the new retrieval's workflow.

## Status, logs and results

Retrieval retains its one-second status polling. Workflow status uses the
server's poll_after_ms. useWorkflowLogs reads stderr with the server's UTF-8 byte
cursor every 1.5 seconds, immediately draining additional pages. On a terminal
transition it reads the remaining tail before stopping. Log rotation resets the
cursor; connection errors retain content and reconnect every three seconds.
None of these actions replay a task. The browser keeps the most recent 65,536
characters; the complete terminal log remains downloadable. Follow can be
paused, and scrolling away from the bottom pauses it automatically. Error lines
are an explicitly labelled keyword filter, not a structured error taxonomy.

Counts represent source records, not generated QA. Unknown progress remains
indeterminate. The existing workflow error/status is displayed without invented
model stages or percentages.

New authenticated, batch-scoped read-only endpoints:

- GET /batch-retrieval/jobs/{id}/preview?offset=0&limit=10
- GET /integrations/data-work-flow/jobs/{id}/results?offset=0&limit=20
- GET /integrations/data-work-flow/jobs/{id}/files/review
- GET /integrations/data-work-flow/jobs/{id}/files/stderr

The manager exposes these under /data-batches/{batch}/api as with the existing
business endpoints. IDs resolve through the child's owned job store; callers
cannot supply file paths. Package preview shows entities, relationships and
original_texts. Pagination does not change publication or distillation inputs.

QA preview reads the final version of processed work items from the current
review schema 3, manifest schema 4 and difficulty_routed_qa v5/v6 contracts. It
checks the owned run directory, receipt identity and committed review byte
checkpoint. It neither approves nor rewrites QA. Downloads return the entire
original review.jsonl, including revisions, classifications and inspections.
Unsupported or unpublished results report an explicit conflict/error.

## Validation and deployment

Evidence is archived in the parent workspace under
config/validation/batch-flow-ui-20261105. Only the isolated 9631 test service is
updated. Its launcher defaults to that archive's accepted immutable wheel;
served assets are never overwritten in place. The official data-work-flow
checkout and primary 9621 service are outside the change.

The frontend budget guard remains enabled. The new bilingual copy and shared
responsive styles require a 582-byte (0.026%) adjustment to its recorded
baseline; asset-budget-change.json records the observed closure and exact
before/after limits. The page and result previews are separately loaded.

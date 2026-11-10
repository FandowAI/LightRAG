# Multi-batch implementation execution record

The latest checkpoint is the 2026-11-04 remaining-completion entry below. Earlier entries retain the scope and verification boundary at their own checkpoint.

## 2026-11-03: phase 2, isolated runtime

Requested scope: implement isolated runtime execution and record the work after phase-1 offline acceptance. Work was performed in the existing `feat/batch-isolation` checkout. Existing working-tree edits were retained; no commit, service restart or live-workspace migration was performed.

Status at this checkpoint: runtime implementation and local subprocess acceptance were complete. Production Neo4j/Milvus acceptance and phase-3 business-task integration had not been executed.

### Delivered

- Added the startup compiler, persistent process reservations/events, process-ownership backend, runtime supervisor and worker under `lightrag/batch_control/runtime*.py`.
- Used one independent RAG process, stable workspace and resource namespace per batch, with monotonically increasing process generations.
- Checked actual initialized storage/model/concurrency bindings before granting a ready lease; added safe stopping, capacity retention on uncertain exit and recognition after manager reconstruction.
- Exposed the initialized RAG through `app.state.rag` for internal lifecycle observation without changing existing service startup configuration.
- Added runtime regression and real local subprocess tests, and documented interfaces/limitations in [BatchRuntimeIsolation.md](BatchRuntimeIsolation.md).

### Execution evidence

The real local smoke archive is stored at:

`/root/MasterRAG/config/validation/batch-runtime-isolation-20261103/real-local-processes.json`

The archive contains two actual process leases, sanitized observations, persistent runtime events and the verification flags. It excludes the private observation capability and model credential values.

| Check | Recorded outcome |
| --- | --- |
| Real RAG subprocesses | Two different namespace-local PIDs, 84 and 149, with different birth identities |
| Archived smoke time | 2026-11-03 15:51:24, Asia/Shanghai |
| Batch isolation | Different workspace/collection identities and separate local storage paths |
| Manager reconstruction | Existing runtime recognized through registry, process identity and fresh health proof |
| Independent stop | Stopping the first child left the second child ready |
| Cleanup | Both validation children confirmed stopped |
| Model requests | 0 |
| Workflow execution | Not executed |

Validation used temporary roots, synthetic administrator catalogs and injected workflow-schema probes. The local RAG app/storage lifespans and child-process ownership were real. No production control registry, graph label, vector collection or existing service was adopted.

### Automated checks

The phase-1 baseline had 47 passing batch-control tests. The initial phase-2 regression run passed 167 tests across batch control, the DataFlow bridge and its API routes. After final policy-validation and startup-reuse regression additions, those modules passed 172 tests. A separate run of scheduling-health, chunker-startup and server-import checks passed 35 tests. Total passing relevant checks: **207**. Ruff lint, runtime-file formatting and `git diff --check` also passed.

An additional `tests/api/test_health_auth.py` check failed in its first two tests. With deployment dotenv defaults it reached an entity-merge constructor unsupported by its fake RAG. After isolating storage configuration, it failed because `_FakeLightRAG` lacks `user_prompt_prefix`, which the existing `PromptAdmin` constructor requires. The first failure was reproduced with this change's `app.state.rag` addition removed in memory; it is an existing fake-fixture incompatibility. This test file remains failing and was not changed in this scope. An initial supplemental run without the sandbox selector workaround stalled and was interrupted; the correctly configured rerun passed all 35 supplemental checks.

The selected test scope is reproducible from `LightRAG/` with the repository interpreter:

```bash
EMBEDDING_BINDING_HOST='' ENTITY_TYPE_PROMPT_FILE='' ENTERPRISE_KNOWLEDGE_ENABLED=false \
  PYTHON="$PWD/.venv/bin/python" ./scripts/test.sh \
  tests/batch_control tests/integrations/data_workflow \
  tests/api/routes/test_data_workflow_routes.py \
  tests/api/test_health_scheduling_view.py \
  tests/api/test_custom_chunker_startup.py tests/api/routes/test_server_import.py -q

.venv/bin/ruff check lightrag/batch_control tests/batch_control
.venv/bin/ruff format --check lightrag/batch_control/runtime*.py tests/batch_control/test_runtime.py
```

The restricted execution environment blocks socket binding/network access. Its pytest launcher caps selector polling at 10 ms to handle unavailable cross-thread socket wakeups; that adjustment is test-launcher-only and is absent from production source. `LIGHTRAG_RUNTIME_VALIDATION_RECORD` was set only when archiving the successful real-process smoke. The archive reflects the completed smoke; later unit-test additions do not change those observations.

No passing result is claimed for `tests/api/test_health_auth.py`. Its fixture incompatibility is separate from the live-database acceptance limitation below.

### Remaining acceptance and later phases

- Neo4j/Milvus live-server isolation: **not executed** because the execution environment does not permit the required connections. The production adapter exists, but it has not passed live database acceptance.
- Ingestion/retrieval/distillation dispatch into managed children, public task/status APIs and frontend presentation: not implemented in this phase.
- Pending-startup-change application, index rebuild, live workspace migration and shared model/token admission: not implemented in this phase.

No active 9621/9631 service was stopped or restarted, no existing workspace configuration was replaced and no model request or distillation job was triggered by this acceptance run.

## 2026-11-03: phase 3, full-chain backend integration

Requested scope: connect the full business chain after isolated-runtime implementation and write an execution record. Work remains in the existing `feat/batch-isolation` checkout; unrelated working-tree changes have been retained. No commit, production activation, default-workspace adoption or existing-service restart was performed.

### Delivered

- Added strict commands and batch-scoped persistent admission/claim/progress/events for ingestion, center selection, evidence retrieval, query-data, distillation and combined chains.
- Connected the existing native upload ingress/document tracking, `BatchManager`, evidence publication/checksum verification, three-field conversion, job-local recipe snapshots and selected workflow CLI receipt verification.
- Bound commands to the authenticated principal, exact configuration/Prompt digests, source ownership and runtime generation. Repeated button clicks use stable request identity; interrupted model work is not replayed automatically.
- Added cooperative cancellation, manager reconstruction, safe background-stage/thread cleanup and shared process-handle ownership. Queued work remains durable through transient dispatcher errors.
- Added opt-in authenticated `/data-batches` APIs with independent progress, events, logs and fixed named artifact downloads. The frontend can submit a task once and poll a separate status link; no frontend page/button was added.
- Added a standalone `python -m lightrag.batch_control` entry point that constructs no default-workspace RAG. Existing-service mounting remains optional through `LIGHTRAG_BATCH_CONTROL_CONFIG`.
- Added an explicit deployment example at `config/batch_control.example.yaml` and the API/operation contract in [BatchFullChain.md](BatchFullChain.md).
- Captured explicit topic catalogs in immutable revision resources. Directory inputs now fail the whole submission on record/byte limits, rather than delivering a filtered subset of supported oversized files.

### Validation boundary and archived evidence

The persistent validation directory is:

`/root/MasterRAG/config/validation/batch-full-chain-20261103/`

| Evidence | Scope |
| --- | --- |
| `local-chain.json` | Real local RAG, native Markdown upload/parse/extraction/storage, real evidence retrieval/dedup/publication, real conversion and a real subprocess implementing the CLI receipt protocol |
| `real-workers.json` | Two actual isolated RAG workers consume persistent directory-distillation tasks; manager reconstruction, distinct batch outputs, independent stop and confirmed cleanup |
| `sample-workflow-input.jsonl` | The exact text-only input record retained from the local-chain test |

The local-chain case processed one document, published one retrieval package and delivered one source to the CLI fixture. Deterministic in-process model functions were called for extraction and embedding; no remote model was called. The worker case used separate PIDs/workspaces and separate workflow output directories; its `all_children_stopped` flag confirms cleanup.

**The CLI subprocess is a protocol fixture, not real QA inference in the official workflow.** Workflow schema preparation is injected for the synthetic test checkout. These checks validate adapter behavior and process ownership without spending model tokens. The production path selects the configured real checkout/interpreter and verifies its terminal receipt, but live external-model inference and Neo4j/Milvus server isolation remain unexecuted.

Native RAG storage, upload ingress, center evidence packaging, checksum verification, projection and CLI receipt handling are exercised rather than replaced by a single mocked success response. Test directories and downstream outputs are temporary; the evidence JSON and input example above are retained separately for review.

### Automated checks

Final regression scope: **261 passed in 57.51 seconds**. This includes 19 full-chain tests covering real local RAG/ingress/retrieval, real worker task consumption, CLI protocol receipts, separate status APIs, authorization, cancellation, source/revision isolation, deployment opt-in and transient-dispatcher recovery. The standalone factory test verifies that it constructs no default-workspace RAG. Ruff lint/format checks, CLI help and whitespace checks also passed. `validation-summary.json` in the evidence directory records the scope, outcome and archived artifact SHA-256 values.

The checked scope is reproducible on an ordinary host from `LightRAG/`:

```bash
EMBEDDING_BINDING_HOST='' ENTITY_TYPE_PROMPT_FILE='' ENTERPRISE_KNOWLEDGE_ENABLED=false \
  PYTHON="$PWD/.venv/bin/python" ./scripts/test.sh \
  tests/batch_control tests/integrations/data_workflow \
  tests/api/routes/test_data_workflow_routes.py tests/api/routes/test_batch_routes.py \
  tests/api/routes/test_server_import.py tests/api/test_health_scheduling_view.py \
  tests/api/test_custom_chunker_startup.py tests/extraction/test_batch_retrieval.py \
  tests/test_docstring_budget.py -q

.venv/bin/ruff check lightrag/batch_control tests/batch_control \
  lightrag/integrations/data_workflow/execution.py \
  lightrag/integrations/data_workflow/batch_source.py lightrag/api/server_files.py
.venv/bin/ruff format --check lightrag/batch_control tests/batch_control
.venv/bin/python -m lightrag.batch_control --help
git diff --check
```

The restricted-host pytest launcher uses the same selector polling adjustment described in the phase-2 entry. Worker shutdown additionally waits for async work and executor threads under its owned event loop before process exit. An early subprocess stop test exposed absent cross-thread wakeups during executor retirement; the explicit cleanup loop fixed it without releasing an unconfirmed process reservation.

The phase-2 health-auth fake-fixture failures remain outside this change and are not counted as passing. No frontend files were changed during this phase, so frontend checks were not part of this backend acceptance.

### Activation and remaining work

Deployment settings are **not enabled in the current service**. Populate the example's paths, model/key references, source references and immutable workflow version before starting the standalone manager on a chosen port. The standalone app avoids creating a second writer against the existing default workspace.

Live Neo4j/Milvus acceptance, real model distillation, frontend placement, pending-startup-change application/rebuilds, existing-workspace migration, shared provider/token quotas and training-format export remain separate work. No existing service, production graph/vector namespace, ingestion task or retrieval package was deleted or reset.

## 2026-11-03: phase 4, configuration application and settings UI

Requested scope: implement configuration application and settings UI, then write an execution record. Work remains in `feat/batch-isolation`; existing unrelated changes are retained. The implementation contract and deployment/build instructions are in [BatchConfigurationUI.md](BatchConfigurationUI.md).

### Delivered

- Added a durable configuration-application coordinator and separate progress/events APIs. Application preflights the desired revision, drains admitted work, stops only the owned batch runtime, starts a provisional generation and promotes the revision only after verified readiness. Other batches retain their runtime.
- Added explicit failure/retry/discard behavior and actor-scoped idempotency. New admission is blocked while configuration is pending. Queued tasks bound to a retired RAG component are retained as interrupted rather than silently replayed with different configuration.
- Added batch settings/preview/save/copy APIs under the existing configuration authority. Inline Prompt content becomes an immutable revision resource. Public choices expose authorized catalog IDs rather than credential values or arbitrary server paths.
- Added the regular WebUI's **Batch settings / 批次设置** tab and an independent `/batch-settings/` entry for the standalone manager. RAG, retrieval, recipe, Prompt and bridge/input groups share one settings component; application progress is a separate component.
- Added reviewed-preview-before-save, conflict handling, stable retry identity, permission-bound editing and selected-group reuse. Retrieval settings are not copied. Unsaved or invalid drafts block batch switching, and invalid JSON path text survives switching settings groups.
- Added administrator-configured UI build-directory support, root-path injection, no-store HTML and explicit missing-build errors. A service without batch management shows an unavailable-service message without loading a default workspace.

### Acceptance and evidence

The evidence directory is `/root/MasterRAG/config/validation/batch-configuration-ui-20261103/`. `validation-summary.json` records the final command outcomes and evidence hashes.

| Check | Outcome |
| --- | --- |
| Backend regression | 272 passed; batch control, workflow integration, related API/retrieval and documentation-budget coverage |
| Real local application | Target generation 1 → 2; changed concurrency observed in the actual new RAG; another live batch left unchanged |
| Promotion boundary | New readiness verified before the active revision changed |
| Child cleanup | All test children confirmed stopped |
| Frontend full suite | 710 passed across 81 files, rendered with happy-dom |
| TypeScript | `tsc --noEmit` passed |
| ESLint | Passed, with the existing ThemeProvider Fast Refresh warning; zero errors |
| Vite build | Passed, including the separate settings entry and existing first-load audit |
| Built asset serving | Actual staged HTML and emitted JS/CSS returned HTTP 200 through ASGI under `/proxy`; HTML was no-store |
| Mutation check | Removing the dirty-draft selector guard made its rendered test fail; source restored before final checks |
| Python style | Ruff lint/format passed for batch-control code/tests and related adapters |

`real-apply.json` archives sanitized leases, application states/events and cleanup proof. `built-ui.json` records the actual emitted-asset check; `ui-mutation.log` records the intentional negative test. Test output, type/lint/build logs and final source digests are retained beside them. No credential values or private observation capability are included.

The backend regression uses the same scope and restricted-host selector adjustment documented in phase 3, with `LIGHTRAG_APPLICATION_VALIDATION_RECORD` pointing to this phase's archive. The selector adjustment is test-launcher-only; runtime production code is unchanged. Frontend checks run from `lightrag_webui/`:

```bash
/root/.bun/bin/bun test
/root/.bun/bin/bun x tsc --noEmit
/root/.bun/bin/bun run lint
/root/.bun/bin/bun run build --outDir /tmp/batch-settings-webui
```

### Activation and limits

The completed bundle is staged at `/tmp/batch-settings-webui`; current served assets are untouched. No existing service was restarted, no deployment was enabled, and no live-workspace data or configuration was migrated. The example still requires administrator-owned paths, model/key/input references and an immutable workflow version before activation.

The process acceptance uses real local RAG workers with deterministic model functions and no external model requests. UI rendering uses happy-dom and the built-asset check uses in-process ASGI; a browser visual review was not executed. Live Neo4j/Milvus, real QA inference and production deployment remain unaccepted.

A failure before stopping the old runtime leaves it running. A later failure retains the previous **active revision pointer**, but the old process may already be stopped; automatic process rollback is not promised. Retry/discard are explicit. Embedding profile changes are rejected after provisioning; this phase does not rebuild indexes. Business-task page placement, live-workspace adoption, provider quotas and training-format export remain separate work.

## 2026-11-03: phase 5, parallel execution and release validation

Requested scope: execute parallel and release validation and write an execution record. Work remains in the existing `feat/batch-isolation` checkout, based on `99968ad`, with all prior working-tree changes retained. No commit, version bump, external publication, existing-service restart or default-workspace migration was performed.

Status: **local parallel acceptance and release-package validation passed**. Production/live-model/live-database acceptance remains unexecuted. The protocol, repeatable commands and acceptance boundaries are documented in [BatchParallelReleaseValidation.md](BatchParallelReleaseValidation.md).

### Parallel execution results

Added three gated real-process cases in `tests/batch_control/test_parallel_release.py`. Both managed RAG workers and both downstream CLI subprocesses must be alive and both tasks running before either CLI is released. The downstream CLI is a deterministic protocol fixture, not official QA inference. Each case uses two independent sources, recipes and captured Prompts.

| A's outcome | B's outcome | Other verified behavior |
| --- | --- | --- |
| Completed | Completed | Both tasks overlap; only A changes runtime generation after draining |
| Failed, CLI exit 2/no receipt | Completed | B remains alive during A's failure and configuration application |
| Cancelled after CLI spawn | Completed | A's CLI is terminated; B remains alive and retains its original revision |

All three cases verify concurrent idempotent submissions, one CLI launch per task, dispatcher reconstruction without replay, rejection at the two-process capacity limit, cross-batch task/artifact isolation, and different converted input hashes. A's pending configuration first reports `draining`; only after its task terminates does A move from generation 1 to 2. B's lease remains unchanged. Every RAG and CLI test child is confirmed stopped.

The conservative observed overlap lower bounds are approximately 1.50, 1.54 and 1.55 seconds for completed, failed and cancelled cases respectively. These are correctness observations with deliberately blocked CLIs, not performance or model-throughput measurements.

### Release results

Added `scripts/release/validate_batch_release.py`, which builds from a private source/UI snapshot, checks packaged module/asset hashes, unpacks the wheel and verifies it under isolated Python import mode from a different working directory. The standalone manager starts/closes its coordinators without constructing a default RAG. The authenticated batch-list request returns 200, an unauthenticated request is denied, and packaged settings HTML plus four emitted JS/CSS references return 200 under `/release`. CLI help is verified from the packaged module.

Accepted candidate and detailed package evidence:

`/root/MasterRAG/config/validation/batch-parallel-release-20261103/release-final/`

The wheel is `dist/lightrag_hku-1.5.8-py3-none-any.whl` (5,479,280 bytes). Its SHA-256 and verified Python/UI hashes are recorded in `release-final/release.json`. The version is unchanged and the wheel has not been uploaded or installed over the current service. Earlier `release*` attempt directories are diagnostics; **`release-final` is the accepted candidate**.

The launcher was corrected during verification for the checkout's absent legacy requirements file, available setuptools backend, API argument parsing and restricted-host executor shutdown. Final checks below use the corrected launcher; no production runtime behavior was changed in this phase.

The actual selected workflow checkout `/root/MasterRAG/data-work-flow`, commit `bbafde58194cc989d4b0420ebfbf6fe112cb60d4`, also passed schema-3 inspection and offline preparation of the active `/root/MasterRAG/config/data_workflow_job.yaml` through its configured `.venv-qa` interpreter. This used placeholder credentials and did not call `run_job` or a model. `official-workflow-preparation.json` records the recipe digest, preparation result and execution fingerprint.

### Regression and archived evidence

| Check | Final outcome |
| --- | --- |
| Relevant backend regression | **275 passed in 132.91 seconds**, including all three new real parallel cases |
| Full frontend regression | **710 passed**, 81 files |
| TypeScript / ESLint | Passed; zero lint errors, one existing ThemeProvider Fast Refresh warning |
| Frontend production build | Passed, including both settings/admin entries and the existing first-load audit |
| Python lint/format | Passed for batch control, its tests and the release validator |
| Packaged manager / assets / auth / CLI | Passed using the actual unpacked wheel |
| Actual selected workflow preparation | Passed against schema 3 / commit `bbafde58194c`, with placeholder credentials |
| External model requests | 0 |
| RAG and CLI child cleanup | Confirmed in all three parallel archives |

The evidence root is `/root/MasterRAG/config/validation/batch-parallel-release-20261103/`. It contains `parallel-completed.json`, `parallel-failed.json`, `parallel-cancelled.json`, final command logs, built WebUI, the accepted wheel/package smoke, and `validation-summary.json` with source/evidence hashes. Backend scope is the same phase-4 regression subset plus the new parallel test file. The restricted-host selector polling adjustment is confined to validation launchers and is not part of production runtime configuration.

Live Neo4j/Milvus isolation, actual official-workflow model inference, provider/token quotas, dependency installation in a clean environment, browser visual acceptance and a listening production HTTP deployment have not passed acceptance in this phase. Therefore this record approves the local candidate only; it does not mark production release complete. Current 9621/9631 services, served WebUI assets, graphs/vectors, document tasks and retrieval packages remain untouched.

## 2026-11-04: remaining-completion goal, workbench code checkpoint

The user requested completion of all remaining work and authorized adoption of
the existing data after its tasks had ended. The full objective remains active.
[BatchCompletionPlan.md](BatchCompletionPlan.md) preserves the remaining
deliverables and the final acceptance requirements; this checkpoint does not
declare the overall system complete.

Implemented an authenticated `/data-batches/{batch}/api/...` business bridge
using a private HTTP mailbox rather than additional listening ports. The actual
owned RAG child handles the document, graph and query requests. Mailboxes bind
the request to its lease/generation, verify request bytes, stream response
chunks, propagate cancellation and participate in runtime drain. Authentication
and manager/configuration-administration paths are excluded from forwarding.

The WebUI now has global batch selection and an explicit **Enter workbench**
action. Selection waits for that batch's readiness. Document/graph/query views
remount on a switch, superseded requests are aborted/rejected, paginated-request
deduplication includes the batch, and batch conversations have separate history
keys. New task controls submit ingestion, center selection, retrieval,
distillation or combined chains to the displayed batch. Task progress/history
is a separate component, with cancellation and explicit idempotent retry.
Unsaved configuration blocks selection, and leaving settings cancels its
pending entry operation so a late result cannot change the active batch.

Verification completed during this checkpoint:

- Relevant backend regression: **287 passed in 150.94 seconds** before the
  final HTTP-drain/scheduling refinements. Those refinements and the additional
  retry case are checked in the final focused HTTP/application/doc-budget run,
  archived separately; these are overlapping scopes, not counts to add.
- Full frontend regression: **718 passed**, 83 files. TypeScript, lint and
  staged build passed; lint retains the existing ThemeProvider warning.
- A readiness mutation (removing the awaited readiness call) made the rendered
  entry test fail. Source was restored and the final build regenerated.
- Actual local workers returned their own health/workspace and graph results
  through the bridge; all test children were confirmed stopped. Streaming,
  generation isolation, request tampering, excluded paths, drain and explicit
  retry are covered by the focused tests.

Evidence is in `/root/MasterRAG/config/validation/batch-completion-20261104/`.
The staged WebUI is `/tmp/batch-workbench-webui`. No current served assets or
running services were replaced at this checkpoint, and no remote model was
called by these tests.

Read-only old-data inventory found **1,018 processed, 30 skipped and 2 failed**
document records in `h200_lightrag`, with 1,020 full-document rows and 2,573 text
chunks. The 9631 test namespace lacks those local document snapshots at its
inspected storage path; its graph tracking files remain present. File hashes
and stable-read checks are archived. Neither workspace has been transferred,
rebuilt, deleted or rewritten. Existing-process ownership and live graph/vector
inventory remain prerequisites for adoption.

Next required work includes the manager's full UI/auth shell without an extra
default RAG writer, the inspection/freeze/adoption journal and actual ownership
transfer, remaining request/settings/result isolation, shared model admission,
index rebuilds, configuration rollback, training exports and real target-host
deployment/acceptance. These remain part of the active goal.

## 2026-11-04: manager shell and adoption guard verification

Added a full manager UI/auth shell without constructing a default RAG. The
normal server and manager share the login endpoints, including off-thread
password verification and reserved concurrent rate-limit attempts. The full
manager serves runtime prefix/configuration, login, customization and health;
its HTML declares `batchOnly`, excludes the legacy workspace entry and fails
explicitly when its build is missing. Before selecting a batch, the frontend
mounts batch configuration instead of document/graph/query business views.

Added exact-API-key legacy inspection, owner-specific freeze/release, complete
HTTP/stream draining, pipeline/enqueue/background/retrieval activity checks and
frozen graph fingerprints. Busy timeout retains the fence. Multiple server
workers cannot claim a process-local fence as exclusive ownership. Immutable
adopted bindings preserve canonical storage/input paths, actual graph/vector
scopes and embedding space. Workers check those identities before storage
initialization, then verify the initial frozen graph before accepting work.
The supervisor commits adoption only against the same verified ready lease.
Subsequent graph edits survive later runtime restarts.

HTTP mailbox recovery now marks orphaned requests failed without replaying
them; uncertain stops retain admission records, and completed responses stay
readable. Query parameters and prompt histories are separately persisted for
each signed-in user and batch. Both rendered settings and actual submission
use the injected scoped store; the original entry retains its original store.

Final verification:

- Batch-control and docstring scope: **153 passed in 135.26 seconds**.
- API authentication/login/health/entry scope: **151 passed in 41.53 seconds**.
- Full frontend: **723 passed**, 83 files, 29.57 seconds.
- TypeScript, lint, staged build and backend Ruff: passed. Lint has only the
  existing ThemeProvider Fast Refresh warning.
- An actual local child reused a seeded existing graph/document snapshot,
  proved the initial graph fingerprint and preserved the original text.
- Two rendered mutation checks failed as expected: removing the empty-batch
  boundary mounted business content; reading global query settings sent top-K
  11 instead of the selected batch's 73. Both mutations were restored before
  the final frontend checks/build.

The auth regression initially exposed stale minimal server doubles. They now
retain constructor configuration, provide prompt-cache hooks and use temporary
local storage rather than inheriting the developer's live backend settings.
Authentication assertions and concurrency coverage remain intact.

Archive:
`/root/MasterRAG/config/validation/batch-completion-20261104/manager-adoption-guards/`.
The staged build is `/tmp/batch-manager-webui`; served assets were not replaced.
No external model request was made. Socket connection and listening-bind probes
both returned `EPERM`; no process absence was inferred from that tool boundary.
No production data transfer or service replacement occurred.

This checkpoint implements and verifies prerequisites. Actual journaled
ownership transfer, backups, registration/exclusive writer coordination,
retrieval-default propagation, result/link auditing, the remaining quota/index/
rollback/export features and real deployment gates remain open in
[BatchCompletionPlan.md](BatchCompletionPlan.md).

## 2026-11-04: rollback, query defaults and task-output access

Completed the durable configuration recovery implementation. Preflight failure
retains an untouched original process only after verifying it still implements
the active RAG component. A replacement failure transitions through
`rollback_stopping`, `rollback_starting` and `rollback_verifying`. Only a
matching owned ready lease and configuration CAS can clear pending admission
and report `rolled_back`. Unknown/unconfirmed shutdown remains pending; an
unverifiable previous runtime is `rollback_failed` with both errors retained.
Coordinator reconstruction does not duplicate an existing rollback child.
Original desired revisions and task/data histories are preserved. The settings
page retains the latest application outcome, and a confirmed terminal failure
gives the next explicit retry a new request identity.

An actual local-process acceptance case started two independent batches,
rejected the replacement's incorrect readiness identity, stopped that owned
replacement and restored the original configuration in a third generation.
The peer retained its original lease. All children were confirmed stopped.
No external model was called. The same case was rerun against the final Python
source to archive its lease/observation and cleanup evidence.

The batch query page now reads active ordinary-retrieval defaults before
rendering its initial send form. Browser overrides and prompt histories belong
to the user, batch and defaults profile. Returning from settings refreshes the
profile; an earlier override does not shadow a changed profile. Invalid known
parameters show an error and reload action. Desired settings are not used as
active defaults. Existing in-flight sessions are retained while refreshing a
valid profile.

Added a fixed artifact catalog and integrated authenticated file downloads,
final result inspection and paginated logs into the independent progress module.
Workflow logs retain their stdout/stderr channels. Worker logs require batch
audit permission and use the task's recorded generation after later restarts.
Catalog/download reads reject foreign tasks and invalid receipt directories.
Terminal tasks without workflow results produce an empty catalog. HTTP drain
now reconciles a confirmed-lost owner before testing pending mailbox requests,
so an orphaned request does not leave configuration application waiting forever.

Frontend request scope is explicit and supports StrictMode effect replay.
Batch switches cancel pending reads, hide prior output and prevent an old
cancellation continuation from fetching/updating the new progress module. An
already-accepted mutation is not automatically replayed.

Final checks:

- Batch-control/docstring scope: **163 passed in 158.09 seconds**.
- Full frontend: **736 passed**, 86 files, 29.47 seconds.
- TypeScript, ESLint, staged production build, backend Ruff and diff whitespace
  check passed. ESLint retains only the existing ThemeProvider warning.
- Five rendered mutations were detected: lost rollback retry control, ignored
  server query defaults, output context leakage, an old cancellation follow-up,
  and a reused retry identity. All source bytes were restored.
- Final-source real rollback evidence rerun: **1 passed in 22.46 seconds**;
  this overlaps the 163-test scope and is not an additional coverage count.

Archive:
`/root/MasterRAG/config/validation/batch-completion-20261104/rollback-query-outputs/`.
The build is staged at `/tmp/batch-rollback-outputs-webui`; current served assets,
9631 launch candidate, production processes and old datasets were not replaced.
Actual ownership transfer, shared quotas, explicit index rebuilding, training
export and live model/database/browser/deployment gates remain open.

## 2026-11-04: journaled legacy ownership transfer

Implemented `adoption.py` and the independent snapshot, backup, provider/resource
and writer-claim modules. Deployment-owned source declarations pin identities
and credential references. Authenticated headless request/status/step/abort APIs
and `python -m lightrag.batch_control.adopt` provide resumable operation without
adding an unrequested frontend migration button.

The coordinator verifies a source freeze, exports full graph/vector data,
confirms the exact source process exited, acquires the physical writer lock,
backs up original storage/input/retrieval/resources/settings and atomically
registers the batch, initial revision, storage binding and adopted profiles.
A durable owner marker prevents accidental legacy or foreign-registry starts.
Crash recovery retains partial copies and journals. Only a matching owned ready
child with graph/vector/document/resource evidence can complete adoption.
An uncertain signal intent is never replayed without an explicit retry.

Current parsing directives, JSON extraction mode, actual role/provider options,
effective extraction settings and private prompt/identity resources are retained.
Prompt files keep their basename contract inside a pinned directory tree.
Initial resource comparisons apply once; later published Prompt changes remain
valid across managed restarts. Original retrieval histories participate in
runtime drain even when they use the ordinary workspace output directory.

Verification against the current Python source:

- Batch-control/docstring scope: **182 passed in 166.70 seconds**.
- Authentication/login/health/entry scope: **144 passed in 40.68 seconds**.
- Backend Ruff and whitespace checks passed. No frontend source changed.
- A real local unmanaged source completed transfer into a real managed child
  with preserved original document/status data, vector contents, provider and
  template settings, verified backups and confirmed cleanup. Additional tests
  cover partial snapshots, incomplete vector exports, source configuration
  drift, foreign process identity, stop replay prevention, authorization and
  pre-registration abort after the owner marker has been written.
- All 10 files from the existing production inventory still match their prior
  hashes. This was read-only; no production ownership was transferred.

Archive: `config/validation/batch-completion-20261104/adoption-transfer/` in the
parent workspace. The real-process acceptance uses local test stores and a
private test-owned ASGI transport; no listener or external model is involved.
The tool sandbox still rejects socket creation with EPERM, so actual target-host
shutdown, Neo4j/Milvus transfer, live model/browser validation and service
deployment remain unverified. The current 9631 candidate was not replaced.
Shared quotas, explicit index rebuilding, training exports and final whole-goal
acceptance remain required in `BatchCompletionPlan.md`.

## 2026-11-04: shared provider quotas and progress

Added the portable provider-admission core, async SDK response lifecycle adapter,
synchronous workflow adapter and LightRAG-owned launcher. Admission belongs to a
canonical endpoint/credential pool and is shared across batches and interpreters.
Concurrency, optional RPM/TPM caps, FIFO, bounded usage parsing, cancellation and
confirmed owner-exit recovery use a separate SQLite authority. Unknown outcomes
retain charges; no quota timeout is treated as proof a live request ended.
Existing upstream retry ownership is retained. Local admission rejects before
sending rather than adding a paid retry layer.

Managed workers report the installed admission binding in readiness evidence.
The selected data_studio CLI still uses its own checkout, arguments, env-file,
workflow, schema, Serving retry loop and terminal receipts. No source file in
data-work-flow was changed. A fresh git status was clean at commit
`bbafde58194cc989d4b0420ebfbf6fe112cb60d4`.

Scoped status APIs and running-task responses include provider queue/active
counts. The independent frontend progress module displays those counts and
hides them immediately on a batch switch.

Verification:

- Backend scope `tests/batch_control`, `tests/integrations/data_workflow` and
  `tests/test_docstring_budget.py`: **271 passed in 177.81 seconds**.
- Frontend: **738 passed**, 87 files, 28.68 seconds; TypeScript, lint and staged
  build passed. Only the existing ThemeProvider lint warning remains.
- A rendered status-role mutation failed and was restored byte-for-byte before
  the final frontend checks.
- Two real interpreter processes used the actual OpenAI SDK and official
  ContentOnlyServing with the same endpoint/key and one allowed concurrent
  request. The workflow queued until the SDK response released the slot. Both
  original answers and five-token usages were preserved; both processes exited.
- The strengthened test exposed a buffered/closed response path bypassing stream
  cleanup; admission now settles that response immediately. Final backend checks
  include the corrected lifecycle.

Archive: `config/validation/batch-completion-20261104/provider-admission/` in the
parent workspace. The staged WebUI is `/tmp/batch-provider-quota-webui`; served
assets and the current 9631 candidate were not replaced. All HTTP test responses
come from private transports; no main/test service port or external model was
contacted. The user's latest instruction restricts future actual service work
to test port 9631 and preserves the main running service/workspace. Index rebuild,
training export and final test-only end-to-end/deployment acceptance remain open.

## 2026-11-04: final-version training exports

Added an independent training-export service, bounded portable worker and
authenticated request/list/status/file APIs. The worker runs in the task's
selected workflow interpreter, importing official configuration and review
validators without creating Serving or calling a model. It reads current
manifest v4/review v3/difficulty_routed_qa.v6 evidence and uses the task's
immutable original revision even after current settings change.

Messages, Alpaca and ShareGPT datasets contain exact final QA only. A separate
ordered provenance file records batch/task/revision/run/source/retrieval and
final-version identities, record hashes, original classification and actual
inspection/human evidence. Processed and exact-final-version machine-passed
selection are explicit; revised answers receive no invented second inspection.
The snapshot manifest explicitly identifies export-submission capture, without
claiming retroactive original-workflow evidence sealing.

Evidence drift, incomplete checkpoints, mismatched original lines, unsupported
contracts and output/sample limits prevent the complete publication. The final
marker is published last and verified for every successful download. Tests
exposed and fixed stale success after marker deletion. Confirmed interruption
requires an explicit new export request; a verified marker can recover a lost
completion update. Logs and partials are retained.

The independent task output module exposes the panel only with server-granted
export capability, offers format/policy selection and scoped progress/history,
and downloads authenticated blobs. Errors survive unrelated history reads;
uncertain submissions retain their idempotency key. Tests also exposed and
fixed the disabled-button residue after closing an in-flight panel.

Final validation:

- Backend batch-control/workflow integration/docstring scope: **289 passed in
  256.27 seconds**, including 18 export tests using actual official offline runs.
- Frontend: **746 passed**, 88 files, 29.71 seconds; TypeScript/lint/staged build
  passed with the existing ThemeProvider warning.
- Three rendered mutations covered stale downloads, reopening during submission
  and the server permission gate; all failed and were restored byte-for-byte.
- Ruff and whitespace checks passed. Export formats and manifests are archived
  from the actual positive tests, alongside logs and source hashes.

Archive: `config/validation/batch-completion-20261104/training-export/` in the
parent workspace. Build: `/tmp/batch-training-export-webui`; served assets and
the current 9631 launch candidate were not replaced. The official workflow
checkout remains clean at `bbafde58194cc989d4b0420ebfbf6fe112cb60d4`. The tool
environment still denies socket creation with EPERM; a fresh 9631-only probe is
recorded without inferring service shutdown. No main port/workspace was touched.
Index rebuild, final context/link audit, actual test adoption and real
database/model/browser/9631 deployment acceptance remain open.

## 2026-11-04: isolated vector rebuild engine and binding adapter

Added independent canonical-source sealing, ingestion-compatible projection,
fresh target construction, explicit batch progress, persisted-row verification,
sealed build-receipt validation and pre-initialization vector binding modules.
Canonical graph and actual text-chunk KV rows establish the complete target set;
old vectors are not used as the authority for missing/stale records. The selected
target model/profile/dimension, backend, physical collections and source identity
are frozen in the receipt. The normal vector factory is rebound for future
construction, including the optional name index, while graph/KV/doc-status and
logical workspace objects retain their original identities.

Only fresh, uninitialized target namespaces are built. Existing empty/partial
targets, foreign stores and initialized source objects are refused. All source
rows are validated before writing. Exact ID sets/content/metadata and finite,
nonzero vectors of the selected dimension are checked after flush. Equal counts
alone cannot pass. Source and target artifacts are bounded while writing and
verified against their complete markers. Partial artifacts are retained, but
limits/cancellation/failure never produce a valid completion receipt. Cleanup
discards only the attempt's pending requests and avoids paid flush replay.

The actual Nano reopen test initially failed because serialization/load-time
normalization changed the writer matrix. The engine now verifies through a fresh
normal on-disk loader. Tests also reopen every populated target and compare its
complete vector proof. Milvus tests use the actual storage batching/iterator
implementation with private clients, exercise all four collection roles and
reject incomplete Strong iteration, equal-count wrong metadata and pre-existing
empty targets. These test transports do not establish live database acceptance.

Verification: **340 backend/integration/docstring tests passed in 264.83 seconds**,
including 51 new rebuild/source/binding tests. Ruff/whitespace passed. All 302
frontend source hashes match the prior accepted 746-test staged build; frontend
source did not change, so those checks were not repeated. data-work-flow remains
clean at `bbafde58194cc989d4b0420ebfbf6fe112cb60d4`.

Archive: `config/validation/batch-completion-20261104/index-rebuild-engine/` in
the parent workspace. It includes logs, source hashes and actual populated/empty
local target evidence plus the Milvus test-transport receipt. This checkpoint
does not wire a public rebuild action or activate a service index. Durable
operation/binding persistence, worker capture/drain, process/provider admission,
candidate verification/activation/rollback and API/UI remain required. Neither
main service/workspace nor test 9631/serving assets/launcher was modified.

## 2026-11-04: revision-scoped index authority and prior-runtime restoration

Implemented `index_rebuild/authority.py` and `store.py`. Immutable binding payloads
and build evidence have separate integrity checks; operation identity, source
lease, phase/version and events persist in SQLite. A reservation atomically adds
the candidate revision/binding and closes admission without moving `active`.
Capture/build completion requires dedicated evidence transitions. Activation
requires exact full RAG identity, configuration digest, vector/model/auxiliary
bindings and initial source/build verification before atomically publishing the
new revision/head. Rollback requires an exact previous generation/physical
binding and a source-restoration attestation when capture exists. It retains the
fence if restoration is unverified. No old index is deleted or paid build replayed.

Ordinary application/discard paths reject rebuild operations before signalling
a process. Later settings preview/save inherit the selected index reference.
The first regression run exposed the preview's synthetic manifest omitting
binding metadata; it now carries the original frozen sources, with regression
coverage through settings preview and its API. The worker and supervisor resolve exact
registry/revision/startup binding identity before allowing a vector-only scope
exception. Graph/KV/doc-status remain in their original workspace; adopted base
storage observations/digests are retained.

Added a rebuild-specific phase-gated and authorized prior-runtime startup port.
A real-process cancellation-before-capture test confirms original source exit,
reconstructs the manager, starts the exact prior revision/vector names, preserves
the peer's PID/generation, clears pending only after verified restoration, and
confirms all recorded children exited. The receipt explicitly labels this scope;
it is not a complete rebuild/coordinator or real database/model acceptance result.

Final backend scope: **365 passed in 285.32 seconds**, including 25 new authority
and runtime tests. Ruff/whitespace passed. Frontend source did not change. The
official workflow checkout remains unmodified; no actual service port, serving
asset, 9631 candidate or production workspace was changed.

Archive: `config/validation/batch-completion-20261104/index-rebuild-authority/`
in the parent workspace. Remaining rebuild work is owned source capture/drain,
admitted build-process execution, live initial candidate/source restoration
verification, coordinator/service and authorized API/UI integration. The worker
currently rejects a preflight/build being presented as normal RAG readiness.
The original whole-goal audit and real test-only deployment gates remain open.

## 2026-11-04: owned source capture, admitted builder and live initial proof

Added independent source audit/capture, candidate/restoration verification and
builder runtime/worker modules. The exact ready source generation drains all
accepted business work and actual deferred admin drives, then seals complete
canonical graph/chunk evidence and audits seven canonical document/status stores
under the snapshot/pipeline/store locks. It retains its request fence through
retirement and neither borrows manual-retry flags nor flushes/repairs source data.

Builders reserve the same process capacity as serving children and install shared
provider admission before requests. They hold workspace ownership but enter no
canonical RAG lifespan. Only fresh shadow vector stores are initialized. Their
`index_building`/`index_built` observations do not become ordinary RAG readiness,
and `built` still holds capacity until confirmed exit. Complete archived receipts
can be recovered after exit without a replay. Candidates re-read actual persisted
vectors and complete canonical state before producing the required activation
attestation. Captured-source restoration independently verifies canonical state.
Both initial paths skip migrations before the pristine comparison.

The in-flight cancellation test found and fixed a dedicated builder-queue
shutdown hang. Cancelling its waiting future did not stop the underlying SDK
task; cancelling workers without setting their shutdown flag could keep them
waiting. The builder now explicitly shuts down only its embedding queue, without
canonical storage finalization or paid flush replay. The regression retains
unknown-outcome token debt, proves a single request/no valid partial receipt and
restores the original document/graph/status audit before clearing the fence.

Validation: **387 backend/integration/docstring tests passed in 410.73 seconds**,
22 additional tests, Ruff/format/whitespace passed. Six actual process records and
registry backups cover empty/populated builds, full candidate activation, receipt
recovery after builder exit, capture cancellation and in-flight restoration. All
recorded test children exited; peer generations remain unchanged. Executing-code
fingerprints match all recorded generations. Populated builds use the official
SDK and private model transports, with all three operation-scoped usage charges
verified. No external model or real database acceptance is implied.

Archive: `config/validation/batch-completion-20261104/index-source-build/` in the
parent workspace. Frontend source was not edited; 302 selected TS/TSX/JSON hashes
match the prior accepted 746-test frontend checkpoint. data-work-flow remains
clean at `bbafde58194cc989d4b0420ebfbf6fe112cb60d4`. No service port, production
workspace, serving assets or 9631 candidate was changed. Authenticated rebuild
service/coordinator, public API/UI/progress, final context/link audit, actual test
adoption and real database/model/browser/9631 acceptance remain required.

## 2026-11-05: automatic rebuild controller, APIs and independent progress UI

Added a principal-bound rebuild service and phase controller. Request preparation
freezes target model/profile, source generation, physical target names and the
complete inherited configuration/resources. The automatic coordinator progresses
batches independently and admits one operating thread per batch, with file locks
also serializing managers. Shutdown joins admitted threads. Source requests,
snapshots and actual deferred admin drives participate in drain. Stop intent is
recorded before a single signal; unconfirmed exit is observed without resending.

Request/list/status/cancel/recover/event APIs retain the existing non-guest auth
dependency and explicit batch/profile/credential grants. Cancellation remains
pending until prior-index restoration verifies. Recovery pins its observed
version and only retries restoration; an authorized replacement executor is
audited without rewriting the original actor. Public status/events omit private
worker paths and capabilities, retain source/target version identity and expose
operation-scoped provider waiting. Settings metadata separates rebuilds from
ordinary configuration apply/discard.

The WebUI starts the entire operation from an explicit embedding-profile action
and displays progress/history in a separate module. Written and verified vector
counts stay distinct. In-flight cancellation does not become instant completion.
Unsaved drafts, pending operations and missing permissions block new starts;
uncertain submissions preserve their request/model identity. Batch/client changes
abort reads/actions and discard late responses. English and Chinese labels are
provided, and restoration actions carry the latest observed version.

Validation: **398 backend/integration/docstring tests passed in 533.59 seconds**,
11 additional checks. Three actual local ASGI API/coordinator runs exercise
populated activation, in-flight cancellation/restoration and reconstruction while
the existing builder is running. They use authenticated-principal test injection
and private official-SDK transports, not live network/models/databases. All
recorded children exit, peer generations stay unchanged, three successful-build
SDK calls remain three after reconstruction, and cancellation records one call.
Thread-gate tests confirm independent batch advancement and shutdown joining.

Frontend: **758 passed in 29.62 seconds**; frozen install, TypeScript, lint and
staged build passed, with only the existing ThemeProvider warning. Five rendered
mutations for permissions, stale submissions, verified counts, recovery versions
and uncertain HTTP outcomes all failed and were restored exactly. New staged assets are at
`/tmp/batch-index-coordinator-webui`; serving assets/9631 were not replaced.

Archive: `config/validation/batch-completion-20261105/index-coordinator-ui/` in the
parent workspace. Registry backups and recorded generation fingerprints accompany
process records, JUnit, frontend/mutation logs and source/build hashes. No main
service/workspace or official data-work-flow source changed. The final whole-path
and process-scope audits, actual test-owned adoption, real database/model/browser
acceptance and 9631-only deployment remain open.


## 2026-11-05 process scope and business-route audit checkpoint

PID ownership now binds machine, boot and PID/procfs namespaces before any PID
lookup or signal. Reservation/specification/attachment/readiness must agree.
Foreign or unreadable scope retains uncertainty and capacity; confirmed reboot
on the same machine establishes exit without inspecting an old PID. A terminal
row cannot replace an unconfirmed prior writer. An uncertain launch without an
attached PID retains its reservation. Adoption PID checks and portable provider
owner recovery also reject foreign procfs rather than inferring process exit.

A real witness remained alive while a second tool execution observed it from a
different PID namespace. Ownership stayed unknown without a PID lookup or signal;
the original owner then released the witness normally. The witness and archived
worker PIDs are not interpreted or signalled from later tool namespaces.

The business-path audit found the document page's entity-merge settings excluded
from batch routing. The shared client and private business mailbox now include
that route. The parent enforces read/edit/paid-preview permissions; the child
uses its private generation capability for ASGI authentication and strips caller
credentials. Header names are normalized to lowercase for ASGI: the capitalized
Content-Type regression otherwise yielded a 422 JSON-body error. The real route
and settings SQLite test preserves a separate workspace. Dialog read/save/preview
requests abort on close or selection changes and discard obsolete continuations.
See [BatchBusinessScopeAudit.md](BatchBusinessScopeAudit.md) for the reviewed paths
and the limits of local evidence.

Final clean checks: **419 backend/integration/docstring tests passed in 532.24
seconds**, no failures/errors/skips, process exit 0. **763 frontend tests passed
in 30.53 seconds**, plus frozen install, TypeScript, lint and staged build. Lint
retains the existing ThemeProvider warning. Six rendered mutations were detected
and original production-source bytes restored. An initial full-suite StrictMode
failure was not reproduced in isolated/diagnostic runs; the test now explicitly
settles async rendering before a simulated user click. The first backend run
also passed 419 tests but its logging wrapper exited 120; the accepted rerun uses
shell redirection and records a successful pytest result and clean process exit.
Intermediate failure logs remain archived.

A fresh wheel validates every packaged Python/UI file and imports from its own
isolated installation. Its ASGI smoke verifies both full workbench and settings
entries, 11 JS/CSS requests, batch-only runtime HTML, authentication, no default
RAG/workspace, blocked legacy workspace HTML and closed coordinators. This is
package/ASGI acceptance, not a real browser or live service deployment.

Archive: `config/validation/batch-completion-20261105/scope-and-business-audit/`
in the parent workspace, including four actual process records/SQLite backups,
all recorded operation-generation code fingerprints, source/build hashes, JUnit,
frontend/mutation logs and the immutable `release-candidate/`. Local process runs
repeat legacy transfer and complete/cancelled/reconstructed index rebuilds, with
original states preserved and recorded children stopped. data-work-flow remains
clean at `bbafde58194cc989d4b0420ebfbf6fe112cb60d4`.

No serving assets, 9631 candidate, main service or main workspace were replaced.
A read-only TCP probe of 127.0.0.1:9631 failed with EPERM in this tool environment;
that is not proof of service availability, exit or idle state. Remaining full
goal gates are target-host test-owned adoption, real multi-batch Neo4j/Milvus and
model acceptance, deployed authentication/browser acceptance, updating 9631 and
its launcher to the accepted candidate, and the final target-host requirement
audit. Production operations remain excluded. The overall goal remains open.


## 2026-11-05 accepted test-candidate launcher checkpoint

Aligned the dedicated 9631 launcher with the accepted scope/business-audit wheel
and added explicit release selection and read-only verification. Candidate checks
cover the wheel, all installed Python/UI bytes, exact file inventories, current
source bytes and the isolated workbench smoke. No service automatically follows
a changed candidate. Test preparation preserves its previous deployment YAML
before updating the UI directory, rejects foreign control roots/legacy sources
before loading them, and avoids repeated no-op backups.

The explicit manager entry requires a completed test-workspace binding/digest
and matching physical owner marker before port access. It loads the selected
packaged batch manager/WebUI without a default RAG and records success only if its
new child owns the listener. Startup records include process scope, birth and
release identity. Legacy restart recognizes exact server entry forms, checks
explicit workspace/port overrides and actual health scope, and refuses foreign
or already-batched manager processes before signalling. Uncertain ownership,
network denial and incomplete adoption retain the running service and data.

Validation: **34 launcher regression tests passed**, plus Ruff/format and the
actual accepted candidate's `--manager-only --verify-only` check. Launches/signals
in unit tests use fixtures; they are not deployed-service evidence. Existing
package and frontend source bytes match the prior **419 backend / 763 frontend**
checkpoint, so no changed application code is represented as retested here.
See [BatchTest9631Deployment.md](BatchTest9631Deployment.md) for the operator entry
and its source-upgrade/adoption/manager boundaries.

A fresh read-only check found the test registry still lacks completed adoption;
the 9631 TCP probe remains denied by EPERM. The actual test deployment YAML did
not change, no service process was started/stopped, and the main service/workspace
and data-work-flow source were untouched. Evidence is in
`config/validation/batch-completion-20261105/test9631-launcher/` in the parent
workspace. Actual target-host transfer, real database/model/browser acceptance,
9631 deployment and the final target-host audit remain open.


## 2026-11-05 target-host blocker audit

Revalidated the actual accepted candidate and all twelve original deliverables.
The candidate's read-only integrity check still passes and official data-work-flow
source remains unmodified. Local implementation evidence does not close the
missing target-host adoption/database/model/browser/deployment gates; none of the
original full-goal items is redefined as complete by the local test results.

The authorized 127.0.0.1:9631 TCP check again fails with EPERM. The same socket
restriction is recorded in three consecutive goal-turn archives (scope/business
audit, accepted launcher, and this audit). The actual test registry still lacks
completed adoption. Its deployment YAML remains byte-for-byte unchanged.
There is no confirmed live validation process to wait on, and a denied socket or
invisible process cannot authorize stopping the source or transferring storage.

Remaining work requires an execution environment permitted to reach the test
service and its actual database/model endpoints: inspect and freeze the real test
source, execute/verify its journaled adoption, run real multi-batch and browser
gates, deploy on 9631 and finish the original requirement audit. No guessed source
identity, copied main workspace, mock result or additional local status report
substitutes for that evidence. The main service and data remain excluded.

Evidence: `config/validation/batch-completion-20261105/target-host-blocked-audit/`
in the parent workspace, including the fresh TCP/registry check and the twelve-
requirement assessment. This is an external execution blocker, not achievement
of the full objective; the unfinished scope is preserved for resumption.

## 2026-11-05: final authorized 9631 execution and acceptance

Resolved the former sandbox blocker using approved native execution. Inspected
and upgraded only the actual legacy test source, completed journaled adoption
`adoption-f5d04c5934d749e6b5eadbb8303dac44`, verified 17 backup files, and started
the complete batch-only manager with no default-workspace RAG. The adopted
workspace retains 38 graph nodes, 23 edges and its existing vector bindings; its
original document KV/status inventory was empty. The main workspace was not
adopted and the main service was not contacted.

Created two synthetic isolated batches, observed concurrent real extraction and
provider requests, verified own-text queries and foreign-task rejection, then
completed center/retrieval/official QA/export workflows (three training samples
each). A fresh real Milvus four-collection rebuild verified all 20 rows before
activation while the peer lease stayed unchanged. Actual preflight rollback,
valid configuration application and restoration, task cancellation and explicit
retry passed. Actual Firefox UI switching/settings/independent-progress checks
had no page errors; password login was not exercised or changed.

Live execution exposed and fixed safe role token-option filtering and strict
current category-v5/topic-v6 export contract selection. The dedicated test
scenario template output now matches its typed parser, through immutable
revisions. Failed attempts and paid-work reuse are documented, not discarded.
No official data-work-flow file was modified.

Final regression: **529 backend tests passed**, no failures/errors/skips, exit 0;
Ruff and whitespace checks passed. All 307 frontend files and the exact source
inventory match the accepted **763-test**/TypeScript/lint/staged-build checkpoint.
Serving/default launcher candidate:
`config/validation/test9631-live-resume/export-fix-release/`; wheel SHA-256
`69878177f4aef420ef9fe42f7185a4a2a5f4b163a340ac5fd4d4a86fd6308cc3`.
Read-only candidate verification passes. Actual final API audit finds four
test batches, all tasks terminal, no pending configuration and healthy manager.
Temporary browser-session credentials were removed from the evidence archive.

All twelve test-service deliverables are accepted in
[BatchCompletionAcceptance.md](BatchCompletionAcceptance.md). Detailed evidence
is retained under `config/validation/test9631-live-resume/` in the parent
workspace. The former blocked records remain historical; no authorized work
remains and no production rollout is implied.

## 2026-11-05: restore original pages and default paragraph chunking

The user's review corrected the interface scope: retain only upper-left batch
selection/creation and preserve all original business pages. Restored the
original BatchRetrievalView and PromptManager; removed the batch-settings tab
from the main navigation. Added batch-owned Prompt/workflow business routing,
restoration-key isolation and workspace business recipe/Prompt resources so the
original controls work without modifying official workflow files.

The Documents page's existing Graph ingestion settings now exposes chunking
strategy, size and overlap. New and existing test batches default to paragraph
semantic chunking (2000/100), with explicit per-document choices taking
precedence. Both file and managed plain-text defaults reach the actual original
chunker. Embedding references and historical documents/graph/vectors remain
unchanged; only future imports use the new settings.

599 backend tests and 766 frontend tests passed. TypeScript/lint/staged build
and immutable candidate verification passed; the original ThemeProvider warning
remains. A rendered mutation catches applying the source instead of target
revision. Actual new-batch browser entry exposed the former two-process test
limit; the dedicated 9631 capacity is now eight, preserving model/provider
quotas. The main service and official workflow checkout are untouched.

Current candidate and full behavior contract:
[BatchWorkbenchCompatibility.md](BatchWorkbenchCompatibility.md). Evidence:
`config/validation/original-pages-restore-20261105/` in the parent workspace.

Actual follow-up acceptance completed: Firefox verifies the original five tabs,
upper-left new-batch entry, original retrieval/PromptManager controls and the
paragraph-default document dialog. Original recipe save and Prompt draft writes
succeed. A real ordinary Markdown import completed with paragraph_semantic
metadata. Final inspection confirms paragraph 2000/100 defaults for all six test
batches and no pending configuration. Historical data was not rebuilt. The
browser's absent WebGL2 context limits graph-canvas verification; the original
graph component is unchanged. Full receipts: `final-acceptance.json` and
`live-original-interfaces-completed.json` in the restoration archive.

## 2026-11-05 — accepted four-step retrieval workbench

Implemented the approved Select entities → Batch retrieval → Check packages →
Distill QA route. Removed the separate fifth result step. Both configuration
entries are available before task creation; the right monitor remains mounted
across step changes and automatically updates status/logs. QA preview, complete
review JSONL and full log downloads live in the monitor. Existing document,
graph, ordinary retrieval, prompt and upper-left batch controls are preserved.
The implementation boundaries and endpoints are in BatchRetrievalWorkbench.md.

Backend validation: 129 passed in tests/integrations/data_workflow,
tests/api/routes/test_data_workflow_routes.py and test_batch_routes.py.
Frontend: 774 passed across 92 files; frozen dependency install, TypeScript,
lint and staged production build passed. The existing ThemeProvider warning is
unchanged. A deliberate disabled terminal-log drain makes its rendered test
fail; the exact source was restored before full acceptance. The first-load
asset guard remains enabled, with its small baseline adjustment explicitly
recorded in asset-budget-change.json.

Deployed only the verified immutable candidate at
../config/validation/batch-flow-ui-20261105/release to port 9631. The prior idle
manager and owned children were stopped only after matching live process birth,
command digest, listener, runtime lease and fresh idle business heartbeat. Test
storage was retained. The launcher now verifies and starts the new candidate.
Wheel SHA-256: 8bfdc634a57bfe8dc37b6c9e2a1493bd31f9201e635dede1bce4342d5870a3bf.

Actual Firefox verification covers English/Chinese UI, four steps, configuration
before submission, all three package fields, independent automatic byte-cursor
logs, step switching, completed-task restoration, real QA preview and exact
result/log downloads. Mobile viewport 390px has no horizontal page overflow.
Changing the data batch removes the previous QA and log content. Browser runs
record zero page errors and HTTP errors on the accepted workflow.

The original UI-restoration document has English entity types classified as
review-tier, so its recommended-only selector correctly offers no candidates.
That candidate policy was retained. For the live distillation acceptance, reuse
the previously accepted synthetic publication 5a4699f01c29479c812c60d87978b6d0
in test batch batch-a400c6ff24014c61bf3abe0adbee6cb0. Task
distill-2b5ba8633a584062b8c0bc8253cd9067 completed one real processed QA. Its
frozen recipe keeps questions_per_source=1; the saved configuration was restored
to 3 while it ran, and verified unchanged after completion. This acceptance
reused evidence; it does not claim a new live candidate-selection/retrieval run.
Those unchanged submission behaviors retain their rendered and API regressions.

All receipts, screenshots and test logs are in the parent workspace under
config/validation/batch-flow-ui-20261105. The temporary browser session file was
removed. No main 9621 endpoint/process or historical production data was touched.
The official data-work-flow checkout remains clean at
bbafde58194cc989d4b0420ebfbf6fe112cb60d4.

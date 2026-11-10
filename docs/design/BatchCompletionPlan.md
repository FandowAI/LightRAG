# Complete batch workbench and existing-data adoption

This checklist tracks the remaining user-authorized objective, not a replacement
for it. Earlier component acceptance does not establish completion of this list.

## Required deliverables

Latest execution constraint: actual service operations are restricted to the
9631 test service. Do not stop/restart or connect to the main LightRAG service,
and do not transfer ownership of or rewrite its active workspace. Existing-data
adoption and deployment acceptance in this run must use test-owned data and
namespaces. Production rollout is excluded by the user's explicit restriction;
it is not inferred from earlier migration permission.

- [x] Global batch selection, explicit workbench entry and clear current-batch identity.
- [x] Batch-owned document upload/listing/processing and graph browsing/editing.
- [x] Batch-owned query, center selection, evidence retrieval and distillation pages.
- [x] Independent task progress/history/cancellation/retry, protected from stale responses after switching batches or login identities.
- [x] Existing-workspace inventory, configuration/resource snapshots and exclusive ownership transfer that reuses existing documents, graph and vectors.
- [x] Execute existing-data adoption in the authorized test workspace after confirming quiescence and process/storage ownership; retain a recoverable journal and backups.
- [x] Shared model-provider request/token admission across batch processes.
- [x] Explicit index rebuilding with evidence of completeness before index activation.
- [x] Failed configuration application rollback to a verified prior runtime.
- [x] Training-format export with source/batch/revision provenance.
- [x] Real multi-batch Neo4j/Milvus and model-backed end-to-end acceptance, rendered browser acceptance and updated 9631 deployment.
- [x] 9631 test deployment acceptance and a requirement-by-requirement completion audit; preserve the main running service and workspace.

## Business HTTP bridge

The owned RAG children currently expose a private registry-based task channel,
not a network listener. Reuse the existing document/graph/query/recipe route
implementations through an authenticated, batch-owned HTTP mailbox. The manager
checks the principal, batch and runtime lease; the reserved child executes the
request against its own ASGI application. Request/response files belong to the
batch and generation. Streamed responses retain their status/content type and
are delivered incrementally. Login, authentication administration, arbitrary
proxy targets and manager endpoints are not forwarded.

Business requests, streams and accepted background jobs must participate in
runtime drain and recovery. A manager restart or lost process never silently
replays a mutation or paid request. New batch work must not fall back to the
legacy default workspace when an owned runtime is unavailable.

The frontend will bind the existing business components to the selected batch
through a shared request context, remount batch-owned view state on a switch,
abort superseded requests and keep task progress separate. Default-workspace
operation remains explicitly identifiable until it has been adopted.

## Verification record

The starting goal turn inspected the current worktree and confirmed the gaps:
only configuration UI has batch selection; existing business pages retain the
default API paths; managed children have no business HTTP bridge; no existing
workspace adoption is registered. No old storage is modified by this inventory.

## 2026-11-04 code checkpoint

Implemented the generation-bound business HTTP mailbox, incremental responses,
request digests, cancellation and runtime draining; the existing document,
graph and query handlers run inside the selected real RAG child. Added the
workbench selector/entry, remount and abort boundaries, isolated chat histories,
task controls, independent progress and explicit idempotent retry. This is
source/staged-build progress, not live deployment or whole-goal acceptance.

Read-only inventory found 1,018 processed, 30 skipped and 2 failed records in
`h200_lightrag`, with 1,020 full-document rows and 2,573 text chunks. Reads were
stable. The 9631 test namespace has graph tracking files but no local
document-status/full-document/text-chunk snapshots at the inspected path.
Neither workspace has been transferred or rewritten.

Next implementation and audit points:

- Supply a full manager WebUI/auth shell that does not initialize a second
  default-workspace RAG after adoption. Stopping a task alone does not release
  its service's storage writer.
- Capture actual legacy runtime/storage bindings through an authenticated
  inspection/freeze protocol, then journal backup, exclusive transfer,
  registration, readiness verification and recovery. Keep original storage
  identities, failed/skipped statuses, input artifacts and retrieval exports.
- Reconcile HTTP rows whose generation was lost, verify streaming cancellation
  and preserve response delivery when a completed generation retires.
- Finish per-batch query-setting isolation, result/log/artifact downloads and
  source/context display; verify every business path and link uses its batch.
- Implement the remaining shared quotas, index rebuild, process rollback and
  training exports listed above, then execute the real target-host gates.
- Rebuild the accepted candidate and update the 9631 launcher only after the
  new source passes its gates; its previous immutable candidate remains intact.

## Manager/adoption guard checkpoint

The full RAG-free manager UI/auth shell, legacy inspect/freeze/release protocol,
immutable storage bindings, pre-initialization identity checks and one-time
initial graph verification are implemented and locally verified. See
[BatchLegacyAdoption.md](BatchLegacyAdoption.md) for the ownership boundaries
and the still-required transfer coordinator. An actual local worker reused a
pre-existing graph/document snapshot and preserved the document content.

HTTP recovery now retires queued/running rows only when their recorded lease
is lost, terminal or replaced. An uncertain stop retains them. Completed
responses remain readable. No mutation or paid request is automatically
replayed.

Query parameters and prompt history now have separate user/batch storage and
are injected into both the parameter panel and actual query submission. The
original query entry retains its existing settings contract. The manager-only
landing does not mount any original-workspace business component.

Final checks: batch backend/docstring scope **153 passed**; API auth/login,
health and entry-mount scope **151 passed**; full frontend **723 passed**;
TypeScript/lint/staged build passed (one existing ThemeProvider lint warning).
Two rendered mutation checks failed as intended, covering the manager landing
guard and the scoped settings used by the actual query request.

Evidence: `config/validation/batch-completion-20261104/manager-adoption-guards/`
in the parent workspace. This is not live adoption or deployment acceptance.
Socket connection and bind probes are denied with `EPERM` in the current tool
environment; an inaccessible target or invisible PID is not treated as proof
that the existing service stopped. Production services/data remain untouched.

The ownership-transfer journal/backups/registration coordinator, actual old
workspace transfer, retrieval-default propagation, full result/link audit,
shared quotas, index rebuild, rollback, training exports and live acceptance
remain required. The original checklist remains authoritative.

## Recovery, query defaults and task-output checkpoint

Configuration application now retains a verified original runtime after a
preflight failure, or retires a failed replacement and restores the previous
revision through explicit durable rollback phases. Admission reopens only
after a matching ready lease and registry CAS. Unconfirmed stops keep waiting;
failed restoration retains both errors and the pending fence. A reconstructed
manager reuses an already-started rollback generation. The latest outcome stays
visible after successful rollback clears `pending`.

Batch query pages now load active ordinary-retrieval defaults before first
use, refresh them when returning from settings, and isolate browser overrides
by user/batch/defaults profile. Known invalid parameters prevent submission and
offer a reload action. Desired-but-unapplied settings do not become defaults.

The independent task progress module now reads final result JSON, paginates
workflow and audited worker logs, lists published files and downloads them
through the authenticated client. Worker logs are tied to the task's original
generation. Scope cleanup rejects late reads and post-cancellation follow-up
requests after switching batches, including StrictMode effect replay.

Verification: **163 backend/docstring tests** and **736 frontend tests** passed;
TypeScript/lint/staged build passed (the existing ThemeProvider warning remains).
Five rendered mutation cases failed as expected and were restored exactly.
An actual local two-batch process run restored the prior configuration after
bad replacement readiness, preserved the peer lease and stopped all children.
Archive: `config/validation/batch-completion-20261104/rollback-query-outputs/`.

These are implementation/local acceptance results. They do not complete live
deployment or the original objective. Still required: the journaled adoption
coordinator and real transfer/backups/exclusive ownership, shared provider/token
quotas, verified index rebuilding, training-format exports, the final business
path/context audit and real database/model/browser/deployment acceptance.

## Journaled adoption implementation checkpoint

Added administrator-declared legacy sources, durable transfer journals, explicit
request/status/step/abort interfaces and a resumable operator CLI. Source
configuration/resource changes prevent shutdown. A verified source receives
one stop signal, followed by process-exit observation; an uncertain signal
intent is never automatically replayed. Exclusive workspace locks and persistent
owner markers prevent a second writer or accidental legacy restart.

Backups include storage/input files, complete graph/vector snapshots, optional
name-index vectors, original retrieval exports, settings SQLite snapshots and
private resources. Prompt directories are copied as trees, preserving the
filename-only template contract. Existing parsing directives, extraction JSON
mode, actual role/provider options and effective extraction settings are pinned.
Initial batch/configuration/profile registration is transactional and repeatable
after a crash. The first owned child verifies source content, document states
and loaded resource identities before adoption can become ready.

A real local unmanaged ASGI source with existing documents, a failed document
and populated entity vectors completed the full transfer into a real managed
child. Tests verified backups, original document/status preservation, template
and provider settings, old-writer rejection, safe pre-registration abort and
confirmed child cleanup. Current batch/backend/docstring scope: **182 passed**
in 166.70 seconds. This supersedes the earlier statement that the coordinator
was unimplemented; it does not establish target-host execution.

No frontend changed in this checkpoint. No production storage was rewritten,
and 9631 was not replaced. A fresh read-only check matched all 10 inventoried
local files. The tool sandbox still rejects socket creation with EPERM; this
does not establish that any real service stopped. Actual old-data transfer,
shared provider/token quotas, verified index rebuilding, training exports,
final context/link auditing and the real deployment acceptance remain open.
See [BatchLegacyAdoption.md](BatchLegacyAdoption.md) for the operator contract.

## Shared provider admission checkpoint

Implemented a shared SQLite provider ledger used by managed RAG SDK requests
and the official workflow Serving through a LightRAG-owned portable launcher.
No data-work-flow source/schema/batch logic was changed. Pools use canonical
endpoint and actual credential identity, independent of role, model alias,
batch or interpreter. Atomic FIFO admission enforces concurrent requests and
optional rolling request/token caps; stream closure, actual usage, cancellation
and confirmed process-exit recovery participate in accounting. Active slots do
not expire merely because time passed. Unknown outcomes retain token debt.

The worker readiness proof now verifies installed provider admission. Batch/task
status APIs expose scoped queue counts, and the independent progress module
renders shared-capacity waiting separately from active requests. Rendered tests
cover clearing counts on a batch switch. A deliberate missing status role was
detected and the exact source restored before final checks.

Final checks: **271 backend/integration/docstring tests**, **738 frontend tests**,
TypeScript, lint and staged build passed. Lint retains the prior ThemeProvider
warning. A two-interpreter process test used the actual OpenAI SDK and the
selected official workflow Serving against isolated test transports; the second
batch waited for the first to release the single shared slot. Both original
responses and actual usage were preserved and both children exited. The test
also exposed/fixed quota release for already-buffered, closed SDK responses.

Evidence: `config/validation/batch-completion-20261104/provider-admission/` in the
parent workspace. Build: `/tmp/batch-provider-quota-webui`. These checks open no
service listener and make no external model request. The main service and its
workspace were not touched; 9631 was not updated. Remaining authorized work is
verified index rebuilding, training exports, final business/context/link audit,
test-workspace adoption, real database/model/browser acceptance and 9631-only
deployment. See [BatchProviderAdmission.md](BatchProviderAdmission.md).

## Training export implementation checkpoint

Completed independent authenticated training-export request/history/progress and
download APIs and the corresponding terminal-task progress panel. Exports use
the task's original revision and selected official interpreter, validate current
manifest/review/inspection contracts, and project final QA into Messages, Alpaca
or ShareGPT. Separate one-to-one provenance retains batch/revision/source,
original classification and exact final-version inspection status. A revised
answer is not represented as re-evaluated or human-approved.

Frozen evidence, committed byte checkpoints, source associations and artifact
hashes are checked before publishing a final completion marker. Limits fail the
whole export instead of truncating records. Interrupted attempts are retained
without automatic replay. A missing or damaged marker blocks downloads even
after SQLite recorded completion. UI requests and blob downloads are scoped to
batch/task identity; closing and reopening an in-flight panel remains usable.

Verification: **289 backend/integration/docstring tests** in 256.27 seconds;
**746 frontend tests** in 29.71 seconds; TypeScript, lint and staged build passed
(the original ThemeProvider warning remains). Three rendered mutations failed
as expected and original source bytes were restored. The 18 export-specific
backend tests include official offline workflow execution, all three formats,
original-revision isolation, a valid empty selection, permission/cross-batch
rejection, evidence changes, limits, damaged publication and crash recovery.

Evidence: `config/validation/batch-completion-20261104/training-export/` in the
parent workspace. Staged WebUI: `/tmp/batch-training-export-webui`. No serving
assets or 9631 candidate were replaced. data-work-flow remains unmodified at
`bbafde58194cc989d4b0420ebfbf6fe112cb60d4`. A fresh test-only 9631 probe cannot
connect because the tool environment prohibits socket creation (EPERM); this
does not establish the service's state. The main service/workspace was not
contacted or modified. Verified index rebuilding, final business/context/link
audit, actual test-workspace adoption, real database/model/browser acceptance
and 9631-only deployment remain required. See
[BatchTrainingExports.md](BatchTrainingExports.md).

## Index rebuild engine checkpoint

Implemented canonical graph/chunk source sealing, complete vector projection,
fresh operation-owned target namespaces, explicit batched build progress and
persisted-vector verification. Entity, relation, chunk and optional name-index
rows retain the normal ingestion identities. Duplicate/colliding IDs, missing
endpoints, extra/missing target rows, changed content/metadata, invalid dimensions
and nonfinite/zero cosine vectors prevent a verified build receipt. Evidence
limits fail the whole attempt without publishing a valid truncated subset.

The vector binding adapter replaces only uninitialized vector objects and their
future construction factory, preserving graph/KV/doc-status/source identities.
Loaded or foreign stores and pending work are retained on refusal. Cancellation
and model failure discard only this attempt's buffered requests; cleanup never
invokes a finalizer that would retry the failed paid embedding work.

An actual local build/reopen test exposed a difference between the live Nano
writer matrix and normal on-disk reload. Verification now reloads the saved
file before hashing/checking vectors. Actual Milvus storage batching and Strong
iterator/count validation are exercised against private test clients, including
the name index and equal-count wrong-content rejection. This is not a real
Milvus server acceptance result.

Full backend/integration/docstring scope: **340 passed in 264.83 seconds**,
including **51 new index-engine/binding tests**. Ruff/whitespace passed. No
frontend source changed; all 302 frontend source hashes still match the prior
746-test/TypeScript/lint/build checkpoint. Official workflow code remains clean
at `bbafde58194cc989d4b0420ebfbf6fe112cb60d4`. Main/test service processes,
serving assets and the 9631 launch candidate were not replaced.

Evidence: `config/validation/batch-completion-20261104/index-rebuild-engine/`
in the parent workspace. The rebuild feature remains incomplete: durable
operation/vector-binding authority, worker source capture under admission
drain, process/provider admission, verified candidate activation/rollback and
the public API/UI must still be connected. The full business/context/link audit,
actual test adoption and real database/model/browser/9631 acceptance also remain
required. See [BatchIndexRebuild.md](BatchIndexRebuild.md).

## Index revision authority and restoration checkpoint

Added immutable revision-owned vector bindings, separate binding heads, durable
phase/version journals and dedicated source/build evidence transitions. Requests
atomically retain the old active revision while fencing admission. Activation
and restoration transactions require exact full-runtime/configuration/vector
identity, source/build attestations and a matching pending operation. Old queued
tasks are interrupted on index activation even when the model is unchanged;
explicit retry freezes the new index identity. Prior/abandoned index evidence is
retained. Ordinary configuration application/discard cannot take over a rebuild.

Settings preview and publication now inherit the active index reference. Workers
resolve it against the frozen revision and registry before vector initialization.
Supervisor readiness allows only that specific vector workspace exception and
retains graph/KV/doc-status ownership. Adopted base storage records remain intact.
Rebuild-specific prior-runtime startup is phase-gated and separately authorized.

The current full backend/integration/docstring scope is **365 passed in 285.32
seconds**, including 25 new authority/runtime tests. A real local process test
cancelled before source capture, confirmed original source exit, reconstructed
the manager, restored the prior configuration/vector names, preserved a peer
generation and confirmed all test children stopped. Candidate activation tests
use explicit fake observations; they do not establish complete live rebuilding.
Ruff/whitespace passed. No frontend source changed in this checkpoint.

Evidence: `config/validation/batch-completion-20261104/index-rebuild-authority/`
in the parent workspace, including `process-rollback.json`. Still required for
the rebuild feature: actual source capture/drain, admitted builder execution,
live initial candidate/source restoration verification, coordinator/service and
API/UI integration. Worker startup explicitly rejects preflight/build purposes
as normal RAG readiness. No service port, production data or 9631 launch candidate
was changed. The original live adoption/database/model/browser/deployment gates
and final business/context/link audit remain open.

## Owned capture, admitted build and live candidate checkpoint

Connected source capture to the exact original RAG worker. Admission closes
before capture and all accepted HTTP, task, pipeline, background, retrieval and
actual deferred admin drives must drain. Complete canonical graph and seven
document/status/attribution stores are audited under their own locks; source
capture never flushes or repairs them. The source fence remains held through
confirmed retirement. Initial candidate and captured-source restoration skip
startup migrations and compare actual canonical contents before readiness.

Added a dedicated `index_build` runtime purpose using the existing shared process
capacity and provider admission. It holds workspace ownership, initializes only
shadow vectors, labels SDK charges with the rebuild operation, publishes private
progress/receipts and never reports ordinary RAG readiness. Confirmed-exited
builders can recover a complete receipt without another paid attempt. Failure
and cancellation preserve partial evidence; no valid truncated receipt is issued.

An actual in-flight cancellation test exposed a builder shutdown hang: cancelling
the embedding caller left its queue's SDK task running, and generic event-loop
cancellation could be consumed by the queue worker. The builder now explicitly
shuts down its dedicated embedding queue without finalizing canonical stores.
The regression confirms one request, retained unknown-usage token debt, no replay,
no completed partial receipt and full canonical-source restoration before reopen.

Verification: **387 backend/integration/docstring tests passed in 410.73 seconds**,
including 22 additional checks. Six actual process records cover empty/populated
build/candidate activation, receipt recovery after builder exit, capture-stage
cancellation and in-flight cancellation/restoration. The populated build uses
the official SDK with private transports, creates all four canonical projected
rows, verifies all three usage charges, preserves a peer and confirms child exit.
These tests use explicit phase steps, not a public automatic coordinator.
Ruff, formatting and whitespace passed. The 302 selected frontend TS/TSX/JSON
hashes match the prior 746-test frontend checkpoint; no frontend file was edited.

Archive: `config/validation/batch-completion-20261104/index-source-build/` in the
parent workspace. It includes six registry backups, process attestations, JUnit,
logs and executing-code provenance matched against every recorded generation.
Official workflow code remains clean at `bbafde58194cc989d4b0420ebfbf6fe112cb60d4`.
No service port, production workspace, serving asset or 9631 launch candidate was
changed. Still required: the authenticated rebuild service/automatic coordinator,
API/UI and independent progress integration, final business/context/link audit,
actual test-workspace adoption, real database/model/browser acceptance and
9631-only deployment. The original full-objective checklist remains authoritative.

## 2026-11-05 automatic rebuild and independent UI checkpoint

Connected authenticated rebuild request/history/status/cancel/recovery/event APIs
to the durable operation authority. Requests freeze the complete revision,
target profile, inherited resources, exact source lease and fresh physical vector
names. They cannot supply storage paths. Unknown submission retries use the same
request identity. Ordinary configuration apply/discard remains separate.

The automatic coordinator advances batches independently and serializes each
batch across managers with a file lock. It drains accepted source work, requests
owned capture, observes confirmed retirement, admits one builder, verifies its
receipt, retires it, starts the full candidate and commits only after its initial
attestation. A recorded stop intent is signalled once; later steps observe exit.
Cancelled or failed attempts restore and verify the previous index before
reopening admission. Explicit recovery retries only failed restoration, pins its
phase version and records any authorized replacement executor separately from
the immutable original actor.

The settings UI now has a distinct whole-operation start action and an independent
progress/history module. It shows phases, written/verified vector counts, shared
provider waiting and cancellation/restoration state. Unsaved settings, pending
operations and missing server grants prevent a new start. Uncertain submission
keeps its model and request identity. Batch/client scope changes abort requests
and discard late continuations; server action permissions and phase versions
remain authoritative. Configuration metadata no longer presents an index rebuild
as an ordinary pending apply.

Verification: **398 backend/integration/docstring tests passed in 533.59 seconds**,
including 11 additional service/API/coordinator checks. Three actual local API/
automatic-coordinator runs cover complete populated activation, in-flight
cancellation/restoration and coordinator reconstruction during the same builder.
The reconstructed case records one builder reservation and three SDK requests;
the cancellation case records one request and verified restoration. Each
preserves a peer and confirms all recorded children exited. Principal injection
is explicit in these ASGI tests; actual deployed authentication/browser/network
acceptance remains a separate gate.

Frontend: **758 passed in 29.62 seconds**; frozen install, TypeScript, lint and
staged build passed. Lint retains the existing ThemeProvider warning. Five
rendered mutations failed as expected and were restored byte-for-byte, covering
permissions, stale submissions, verified counts, restoration versions and
uncertain HTTP outcomes after a possibly accepted submission.
The staged build is `/tmp/batch-index-coordinator-webui`; served assets and the
9631 launch candidate were not replaced.

Archive: `config/validation/batch-completion-20261105/index-coordinator-ui/` in the
parent workspace, including three registry backups, actual process records,
executing-code provenance, JUnit and frontend/mutation/build records. Official
workflow code remains unmodified. The main service/workspace was not contacted
or changed. Remaining full-objective gates are the final business/context/link
audit, host/process-scope ownership audit, actual test-owned workspace adoption,
real multi-batch database/model/browser acceptance and 9631-only deployment.
The original requirement checklist stays open until those gates are proven.


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

## 2026-11-05 final test-host acceptance and completion

Approved native execution resolved the earlier socket blocker and completed the
original twelve requirements within the explicit 9631-only boundary. The actual
test workspace was journaled, backed up and adopted; two additional isolated
Neo4j/Milvus batches completed real extraction, retrieval, official distillation
and three-sample training exports each. A real four-collection Milvus rebuild
verified 20 rows before activation. Configuration preflight rollback, successful
apply/restoration, cancellation and explicit retry preserved the peer runtime.
Actual Firefox workbench checks passed without page errors.

Final backend acceptance is **529 passed**, zero failures/errors/skips, process
exit 0. All 307 recorded frontend files and the exact source inventory match the
accepted **763-test**/TypeScript/lint/staged-build checkpoint. The final immutable
candidate is deployed on 9631, and its default launcher verification passes.
Every task is terminal and no configuration application remains pending.

The full evidence mapping, issue resolutions, adopted-data counts and acceptance
limits are in [BatchCompletionAcceptance.md](BatchCompletionAcceptance.md).
Archive: `config/validation/test9631-live-resume/` in the parent workspace.
Historical blocked/local-only checkpoints above are preserved; they do not
describe the final deployed state. Main-service rollout remains excluded, the
main service/workspace was not contacted or modified, and the official workflow
checkout remains clean. No required authorized test-service work remains.

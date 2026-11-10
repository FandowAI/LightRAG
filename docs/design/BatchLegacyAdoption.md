# Existing-workspace adoption

Adoption transfers ownership of existing storage. It does not enqueue the old
documents again, change failed/skipped statuses, regenerate embeddings, rename
collections or infer that a hidden process has stopped.

## Implemented boundaries

The normal server exposes `/batch-adoption/inspect`, `/batch-adoption/freeze`
and `/batch-adoption/unfreeze`, plus `/batch-adoption/snapshot`, only to its exact configured API key. General
authentication whitelists and a normal user's JWT do not authorize transfer.
These endpoints are excluded from the batch business proxy.

A freeze establishes a server-boot-specific request owner before draining.
New business HTTP calls are rejected, including document writes and model
requests. Calls already admitted remain counted through their complete ASGI
response, including streams. Pipeline activity, enqueue reservations, reserved
background tasks and nonterminal retrieval jobs must drain. A timeout keeps
the fence; only its request owner can release it. Multiple server workers are
rejected because one worker's local fence cannot prove its peers are drained.

The inspection captures actual storage classes, graph label, vector collection
names, model/dimension identity, effective roles, concurrency, document-state
counts and canonical directories. A drained freeze additionally records full
graph row counts and stable content digests. Credentials are not exported.

`StorageBinding` is an immutable, digest-checked registry record tied to a
batch's original workspace. Configuration manifests include its digest.
Compiled startup preserves the original input/storage directories and forbids
an embedding-space change. Before initializing any storage, the child compares
the actual constructed storage identities with the frozen binding. A different
collection therefore fails before initialization can create or migrate it.

Before accepting its first task, the first child reads the existing graph and
compares it with the frozen baseline. The supervisor requires this evidence,
exact storage bindings and the usual owned process/generation/capability proof
before granting readiness. Adoption is committed only against that same ready
lease. Later restarts preserve subsequent graph edits; they do not compare the
edited graph with, or restore it from, the initial baseline.

The manager now has the normal login/customization/full-WebUI entry without
constructing a default RAG. With no batch selected, it mounts batch settings
and does not mount default-workspace business components. Its runtime HTML
configuration explicitly declares `batchOnly`. A missing build returns 503.

## Journaled ownership transfer

`adoption.py` coordinates administrator-owned `legacy_sources` declarations in
the batch deployment YAML. The workflow recipe and browser never supply paths
or process commands. A declaration pins the source URL, workspace, canonical
storage/input/startup directories, exact command digest, API-key credential
reference and model credential/profile references. Model names, URLs,
dimensions, concurrency and effective role options come from the actual source;
the configured profile references must resolve to the same credentials. Sources
must use the adoption-capable single-worker server in the same host boot and
PID namespace. An older source must be updated before transfer is attempted.
The coordinator also verifies that its mounted procfs represents its own PID
namespace before looking up or signalling the source process. An unreadable or
foreign procfs retains the ownership barrier.

The durable phases are `inspect`, `freeze`, `snapshot`, `stopping`, `backup`,
`register`, `registered` and `ready`. Each step is serialized by an operation
lock; its journal and events live in the control registry. Request identities
are idempotent. A failed step retains its last durable phase and evidence.
Source boot/process/configuration/resource identities are checked again after
freezing and immediately before shutdown. Source parsing directives and
extraction JSON mode are preserved. Arbitrary programmatic chunk functions and
enabled reranking are currently rejected before shutdown.

The source exports complete graph rows and all main vector collections, plus
the name-matching vector collection when present. Vector exports include the
vectors and metadata; Milvus uses a strong iterator and independently checks
the collection row count. Pending vector writes reject the snapshot. Snapshot
manifests bind the source boot, freeze owner, graph content and artifact hashes.
Partial attempts remain as diagnostic evidence, and retries create fresh copies.

The verified source process receives one SIGTERM, then the coordinator waits
for confirmed exit without sending more signals. An interrupted signal intent
requires explicit `retry-stop`; it never infers exit from a failed HTTP request,
timeout or a process hidden in a different PID namespace. The coordinator then
acquires the physical workspace writer lock and backs up local storage, inputs,
remote snapshots, retrieval history (including a separate output directory),
shared settings SQLite snapshots and configured private resources. Prompt trees
are copied and pinned as directories, preserving filename-only template
settings. Backups are private, hashed and fsynced. Original document rows,
statuses, graph labels and vector collection names are reused.

Under that exclusive lock, a durable owner marker is written before initial
batch/configuration/profile registration. The registration and journal update
share one SQLite transaction. A crash between the marker and transaction leaves
the old writer fenced; resuming repeats the registration against the same IDs.
A managed workspace keeps its owner marker when idle or stopped. Legacy or
foreign-registry processes, including multiple-worker starts, cannot reopen it.

The first managed child checks initial graph/vector/document-state baselines
and effective resource identities before readiness. The supervisor independently
checks this evidence and completes adoption only against the matching ready
lease. Later graph edits and published Prompt changes survive restarts; the
initial content/resource baseline is not reapplied after adoption completes.
Frozen resource files remain immutable and are checked on every compilation.

`--abort` is permitted only before registration. It releases the matching live
source freeze, or removes this operation's uncommitted owner marker under the
exclusive lock after confirmed exit. It retains journals, snapshots and backups;
it never restarts the old command or deletes user data. Once registered, an
operator resumes the managed startup rather than transferring ownership back.
Shared settings backups must not be restored wholesale over other active
workspaces; they are retained for deliberate per-workspace recovery.

## Operator and progress interfaces

The source declaration belongs in the deployment file passed to the manager,
not `data_workflow_job.yaml`. See `config/batch_control.example.yaml` for the
commented declaration shape. All declared paths are canonical absolute paths.
An ENTITY_TYPE_PROMPT_FILE remains a basename under the pinned PROMPT_DIR tree;
it is not replaced with an absolute template filename.

```sh
.venv/bin/python -m lightrag.batch_control.adopt --deployment /absolute/deployment.yaml --source existing-test --name "Existing test data" --request-id migration-01
.venv/bin/python -m lightrag.batch_control.adopt --deployment /absolute/deployment.yaml --status ADOPTION_ID
.venv/bin/python -m lightrag.batch_control.adopt --deployment /absolute/deployment.yaml --resume ADOPTION_ID
.venv/bin/python -m lightrag.batch_control.adopt --deployment /absolute/deployment.yaml --abort ADOPTION_ID
```

`--once` advances one phase and returns. `--retry-stop ADOPTION_ID` explicitly
renews an uncertain stop intent after re-verifying the original frozen process.
The authenticated manager exposes POST `/data-batches/adoptions` with
`{source_id,name,request_id}`, GET `/data-batches/adoptions/{operation}`, and
POST `/{operation}/step`, `/{operation}/abort` and `/{operation}/retry-stop`
under the same adoption prefix. Initiation and progress are separate endpoints.
Public status contains only operation/batch/revision identity, phase, timestamps
and sanitized error/wait messages; it contains no paths or credentials. The
operator must have `adopt_workspace` on the declared source, `create` on
`batches`, and the referenced profile/credential/runtime permissions.

## Remaining target-host acceptance

The latest user restriction permits actual service operations only on test port
9631. Any transfer in this run must use test-owned data; the main service and
its active workspace retain their existing ownership and data. Earlier migration
permission does not override this restriction.

The existing service must release ownership before a managed child opens its
storage. A timeout, unreachable HTTP endpoint or a PID not visible in a tool's
namespace does not establish release. The full manager replaces the default
writer when the old default workspace becomes a batch.

The process integration test starts a real unmanaged ASGI source with original
documents, a failed document and a populated local vector collection, then
executes the full journaled transfer into a real managed child using the same
paths. It verifies backups, template/provider/parser settings, unchanged
documents/statuses, initial vector contents, old-writer rejection and confirmed
cleanup. Its private test transport opens no listener and calls no model. This
proves the local transfer path; real Neo4j/Milvus, test-workspace migration, model
calls, browser acceptance and 9631 deployment remain required. No production
workspace is adopted by the local test.

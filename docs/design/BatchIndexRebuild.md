# Verified replacement indexes

Implementation status: canonical source/projection, a fresh-namespace builder,
persisted-row/receipt verification and pre-initialization vector binding are
implemented. Immutable revision bindings, transactional activation/restore and
full-runtime identity checks are also implemented. The admission coordinator,
actual worker source capture, admitted build subprocess, live initial candidate
verification, public API and UI are still required. The registry transactions
alone do not authorize or execute a complete running-batch rebuild.

## Required end-to-end behavior

An explicit rebuild keeps the batch identity and original graph, documents,
statuses, retrieval results and workflow history. It creates new operation-owned
vector namespaces; the old vector files/collections remain intact. The selected
embedding profile may be the existing profile or a different registered profile.
New indexes become active only after complete persisted-row verification and a
matching owned runtime readiness proof. A creation timestamp or record count is
not evidence of completeness. No paid embedding request is replayed automatically
after an uncertain process outcome.

The coordinator must fence new business requests/tasks, drain existing writers
and accepted HTTP work, and obtain canonical source evidence from the exact
owned old generation before retiring it. A failed/unknown stop cannot authorize
the target generation. Runtime process-capacity admission and shared provider
quotas continue to apply. Original graph/KV/source identities stay fixed;
only the vector binding and matching embedding configuration change together.
Activation is transactional in the registry. Failure retains private artifacts
and restores a verified prior runtime/binding before reopening admission.

## Canonical evidence and projection

Source evidence contains the full graph nodes, undirected relations and actual
text-chunk KV rows, including each chunk's real key. Old vector records do not
establish the source set: they may already be incomplete or stale. The owning
worker must maintain the rebuild fence through source capture and retirement;
the file-writing primitive does not itself freeze a workspace.

`index_rebuild/source.py` creates an exclusive private source directory, writes
and fsyncs all artifacts and publishes `source.json` last. Its identity binds
batch, operation, configuration revision, workspace, lease, generation and
startup digest. Hashes/byte counts cover every source artifact; the marker also
records the graph baseline and complete untruncated projection. Existing
directories, symlinks, changed files, duplicate identities, oversized evidence
and mismatching identities fail rather than publishing a partial subset.

`index_rebuild/projection.py` uses the existing ingestion identities/content
layout: entity IDs hash the canonical graph key, relationship IDs hash sorted
endpoints, chunks retain their actual KV key, and optional name vectors use the
current entity-type validator and `ent-name-` identity. A duplicate undirected
relation, missing endpoint or ambiguous concatenated relationship ID fails
explicitly. Source records are not mutated. No new graph extraction or QA
generation is part of vector rebuilding.

The builder must consume and validate the entire projection before writing a
target. A target-model content/token transformation is applied once while
binding a plan, and the transformed content is then immutable expected evidence.
Chunk content must retain the current embedding wrapper's normal semantics.
The builder must refuse an already-populated or foreign target namespace.

Persisted target verification requires the exact expected primary-key set, exact
content and metadata, no duplicate or extra row, correct vector dimension and
finite nonzero cosine vectors. The proof hashes content and actual vectors.
An equal-sized target with one missing and one extra row fails. The coordinator
must bind this proof to the exact target namespaces, model/profile, source
manifest, operation and generation before it can authorize activation.

## Isolated build and vector binding

`index_rebuild/builder.py` binds the exact source manifest, target revision,
embedding profile/hash/model/dimension, backend and physical collection names.
Target workspace names derive from batch and operation identities and must
differ from the canonical workspace. Every target is checked before an upsert;
an existing empty collection or previous partial directory is refused too.
Batching flushes explicitly and records private progress. Failure/cancellation
discards only this attempt's pending requests and closes its channels without
calling a finalizer that could retry a paid embedding request. Foreign or
previously initialized stores are never cleared or finalized by that cleanup.

The builder reloads each Nano target through its normal on-disk loader before
verifying rows. The live writer matrix is not a persistence proof: the Nano
library normalizes again on load, which can change float32 values. Milvus
verification uses Strong consistency for complete iteration and a separate
count comparison, retaining the collection read lease through iteration. The
shared snapshot serializer enforces optional row/byte bounds while writing;
limits fail the whole receipt instead of writing a truncated valid receipt.

`verified.json` is written last after all collection checks. `read_verified`
revalidates source, exact target bindings, every archived vector artifact,
complete metadata/content/vector proofs and counts. A registry status or marker
alone cannot authorize activation; the coordinator must also verify the actual
bound stores and matching live candidate. Empty sources create explicit empty
model-bound indexes and make no synthetic embedding request.

`index_rebuild/binding.py` replaces only vector instances and their future
construction factory before the RAG lifespan starts. Graph, KV, document-status,
input and logical batch workspace objects remain unchanged. The optional name
index follows the same target binding. Late rebinding, foreign workspace/model,
missing auxiliary roles and later cross-batch vector construction are rejected.
Existing loaded stores/pending writes are retained on refusal. No core RAG or
data-work-flow modification is needed for this adapter.

## Persistent authority and runtime checks

Vector bindings must be immutable versions associated with configuration
revisions. A mutable global workspace override is insufficient: rollback must
resolve the old revision's exact old vector names/profile, and historical tasks
must retain their original configuration/source provenance. The initial adopted
graph/KV/storage authority must remain preserved; a verified vector replacement
adds a new authority rather than editing that original frozen observation.
Normal later settings publications must inherit the active vector binding.

The candidate's pre-initialization guard and readiness verifier must require the
exact registered binding, including the optional name index. They may allow the
explicitly bound vector workspace to differ from the canonical workspace; graph,
KV and document-status workspaces must still match the original batch. Applying
that exception globally or accepting a collection-name prefix alone would weaken
the existing isolation contract and is not an activation proof.

An isolated build process must participate in runtime capacity and shared model
admission. Its completed vector build is a separate proof from a full RAG-ready
generation. The coordinator must not fabricate normal RAG readiness for a builder
or let the manager route business traffic to it. It must confirm builder exit,
start the full candidate against the verified binding, verify exact readiness
and source stability, then commit the binding/configuration and reopen admission.
An uncertain stop retains the fence/reservation. A reconstructed coordinator
observes the existing process and receipt instead of starting another paid build.

`index_rebuild/store.py` persists operation identity, source lease, phase/version,
immutable binding payload and append-only events. Reservation atomically adds
the new revision/binding and closes admission while retaining the old active
revision. Cancellation increments the phase version. Generic transitions cannot
invent captured-source or verified-build phases. Dedicated evidence transitions
require exact source generation/PID or owned/confirmed-exited builder identity.
Build evidence has its own integrity digest; abandoned and prior bindings remain
available for investigation instead of being deleted.

Activation requires a matching full RAG lease, exact graph/vector/auxiliary
collections, embedding identity, configuration digest and the initial source/
build verification attestation. It atomically publishes the revision and vector
head, retires the old binding and interrupts old queued tasks, even when the
embedding profile is unchanged. Those tasks froze the earlier index identity;
an explicit retry adopts the new one. Admitted/running tasks prevent commit.
Rollback retains the fence until an exact previous-revision generation and
original physical collections are ready. When capture exists, restoration must
also attest to the captured canonical source state. Failed restoration keeps the
operation pending and requires explicit recovery; no paid build is replayed.

Ordinary configuration application/discard cannot take over a rebuild operation.
Settings preview and later publications inherit the active revision's binding
reference/digest. Compilation resolves that revision's immutable reference;
missing/foreign/abandoned bindings never fall back to a default vector namespace.
Declared bindings can be compiled for non-executable preflight or their private
build phase; a full candidate requires verified evidence and the matching pending
phase. Workers verify registry/revision/startup identity before initializing the
selected vectors. Supervisor readiness permits only that exact vector workspace
exception; graph/KV/doc-status remain in the original workspace. The initial
adopted storage record/digest remains intact.

The rebuild-specific prior-runtime startup port is phase-gated and separately
authorized. Actual process tests cover capture, builder execution, full candidate
readiness/activation, prior-source restoration and reconstructed controllers.
They use operation-owned local stores and private model transports. The public
API and automatic coordinator are described below; real database/model and
deployed-service acceptance remain separate gates.

## Owned source capture and initial verification

`index_rebuild/worker.py` runs inside the exact original ready RAG generation.
It checks operation/generation/lease/startup/PID/birth identity before capturing.
The legacy request fence closes new business admission, while accepted HTTP,
tasks, retrieval jobs, background tasks, pipeline work and actual deferred admin
release drives must drain. The worker then holds the snapshot and pipeline locks
through capture, without borrowing manual-retry/destructive pipeline flags.
Incomplete capture remains private and moves to rollback; it is not reused.

`audit.py` fingerprints complete graph contents and seven canonical JSON stores:
full documents, text chunks, document status, document entity/relation anchors,
and entity/relation chunk tracking. It reads under each store's own lock and never
flushes or repairs source data. Source rows are checked again before publishing
the immutable capture audit and registry evidence. The source admission fence
stays closed until confirmed process retirement.

Before the first full candidate becomes ready, `verification.py` revalidates the
sealed source/build evidence, audits actual canonical stores, reopens/iterates
all actual target vector collections, and compares exact row/content/vector
proofs against the completed build. It checks canonical state again afterwards.
Only then does the worker produce the initial binding/receipt/source attestation.
Restoration after capture independently audits the original canonical stores.
Both paths skip startup migrations until this pristine-state comparison, so a
repair cannot mask source drift. Later restarts of an activated index perform
ordinary startup; the old capture is not treated as a permanent prohibition on
subsequent document or graph edits.

Target artifacts are verified incrementally; only exhausting their iterator
validates the complete artifact hash. Source rows still load and validate fully
before they are used for a target write. A manager-side token transformation uses
the managed API's fixed `gpt-4o-mini` tokenizer and frozen embedding token limit,
and is tested against the actual server-side transformation.

## Admitted build processes

`build_runtime.py` reserves a distinct `index_build` generation through the same
capacity ledger as serving RAG children. It requires the captured source's exact
retired generation and confirmed OS exit. An existing builder is observed, never
restarted or replayed. An uncertain launch retains its reservation. Private
observations bind capability, PID/birth, target/source, profile, provider
admission and freshness. Completed receipts are fully revalidated before the
generation is marked `built`; this state still holds process capacity and never
establishes ordinary RAG readiness. A matching receipt can be recovered after
confirmed builder exit without another embedding request.

`build_worker.py` constructs the same configured vector objects but enters no
canonical RAG lifespan. It holds the canonical workspace ownership claim while
opening only shadow vectors. Shared provider admission is installed before any
request, and every SDK request carries the rebuild operation as its task label.
Progress, partial targets, receipts and logs stay private to the operation.

Cancellation interrupts the builder and explicitly shuts down its dedicated
embedding queue with `graceful=False`. Cancelling only the waiting future is
insufficient: the queue's running SDK task can survive it, and cancelling a
worker before setting its shutdown flag can leave that worker waiting forever.
Queue shutdown never finalizes canonical stores or retries buffered paid work.
Unknown transport outcomes retain their existing token reservation. The process
must still exit before its capacity/ownership can be released.

Actual populated tests build two entity rows, one relation and one chunk through
the official SDK using a private transport, verify all three usage charges and
operation labels, stop the builder and activate a fully verified new RAG while
preserving a peer generation. Another test cancels an in-flight private request,
proves no completed partial receipt or replay, and restores the original graph,
documents and states after their complete audit. Those engine/runtime tests use
explicit phase steps; separate API/coordinator tests exercise automatic progress.

## Automatic requests and progress APIs

`service.py` validates the authenticated principal, expected active revision,
administrator-declared embedding profile and stable request identity. Requests
cannot supply source paths, target collections or workspace overrides. The
complete new revision, inherited prompt/resource references, embedding profile
hash, physical target names and exact verified source lease are frozen before
admission closes. Repeating an uncertain submission returns the same operation.
Milvus target names use the actual storage naming contract, including its model
and dimension suffix. Ordinary settings publication still cannot silently switch
an existing index's embedding space.

`controller.py` advances one durable phase per step, serialized by a private
batch operation lock. Capture completion must come from the actual source
worker; a controller transition cannot invent it. Source/business activity also
covers active requests, source snapshots and actual deferred admin release
drives. Accepted source work drains even when the rebuild itself is cancelled.
Retirement records an intent before signalling once; a timeout retains the
reservation, and subsequent steps only observe that same process until exit.

Build completion requires the exact builder reservation and verified receipt.
A controller reconstructed during a build reuses that generation and receipt;
an exited partial build moves to restoration instead of another paid attempt.
Full candidate and prior-runtime readiness require their dedicated initial
attestations before registry commit. Failed restoration retains the pending
fence. Explicit recovery pins the current operation version and retries only
restoration; an authorized recovery executor is recorded separately from the
immutable original actor.

`coordinator.py` progresses batches independently while allowing one outstanding
step per batch in a manager. File locks also serialize competing managers.
Shutdown joins admitted operating threads; cancelling a `to_thread` waiter is
not mistaken for stopping its thread or storage work. `BatchDeployment` starts
and joins this coordinator alongside the existing task/configuration services.

All routes below are under `/data-batches/{batch}` and use the same non-guest
principal dependency as other batch APIs. Mutations require `edit`,
`rebuild_index`, `run_runtime` and `stop_runtime`; the selected profile/credential
references need their normal grants. Status/history require `read`, and events
additionally require `audit`.

| Method | Relative route | Input/purpose |
| --- | --- | --- |
| POST | `/index-rebuilds` | `expected_revision`, `embedding_profile`, `request_id`; reserve one complete rebuild |
| GET | `/index-rebuilds` | Recent history, default 20 and maximum 100 |
| GET | `/index-rebuilds/{operation}` | Phase/version, target model, verified/written counts, provider waiting and permitted actions |
| POST | `/index-rebuilds/{operation}/cancel` | Request cancellation; restoration is asynchronous |
| POST | `/index-rebuilds/{operation}/recover` | `expected_version`; explicitly retry failed prior-index restoration |
| GET | `/index-rebuilds/{operation}/events` | Sanitized phase history without private worker paths or capabilities |

A request body is, for example:

```json
{
  "expected_revision": "rev-current",
  "embedding_profile": "embedding-v2",
  "request_id": "rebuild-request-001"
}
```

Cancellation acknowledgement is not completion. The operation remains pending
until the original index has been verified and restored. After a completed
rollback, a deliberate new rebuild uses a new request identity and fresh target
namespace; it never reuses partial paid work. Successful activation interrupts
queued tasks frozen against the old index; explicit task retry adopts the new
binding. Rollback preserves those queued tasks' original revision.

## Independent frontend controls

`IndexRebuildAction.tsx` starts the complete operation using the current active
revision and chosen embedding profile. Unsaved settings, pending operations or
missing server permissions disable the action. Uncertain submission retains its
idempotency key and selected model. `BatchIndexProgress.tsx` independently reads
recent history, phase, written/verified counts, shared provider waits and recovery
errors; it may be mounted separately from the action. Cancellation stays visibly
pending until actual restoration. Recovery sends the observed version and never
starts another embedding build.

HTTP 409 can mean an earlier submission is still holding the operation lock,
and a server error can occur while returning status after reservation already
committed. Neither is proof that no task was accepted. Those outcomes retain the
request identity; definite authentication/path/validation rejection can reset it.

Both modules cancel requests and discard continuations on batch/client scope
changes, including StrictMode replay. Responses must identify the expected batch
and operation; older list versions cannot replace newer action responses.
Settings metadata distinguishes index rebuilds from ordinary configuration
application, so the ordinary apply/discard controls cannot take over a rebuild.
Rendered tests and deliberate mutations cover permissions, stale submissions,
verified counts and recovery versions. English and Chinese labels are supplied.

## Remaining integration

- Complete the whole business/context/link audit and actual test-workspace adoption.
- Execute rendered-browser acceptance against the updated test deployment.
- Execute real database/model and 9631-only deployment acceptance separately.

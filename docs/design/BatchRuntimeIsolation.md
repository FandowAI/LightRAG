# Batch control: isolated runtime supervision

This describes the phase-2 implementation following [the configuration contracts](BatchControlContracts.md). `RuntimeSupervisor` starts one RAG lifespan per managed batch in an independent child process. It does not adopt the existing default workspace. The subsequent [phase-3 full-chain adapter](BatchFullChain.md) now adds registry-based task dispatch and opt-in headless APIs; frontend presentation remains separate.

## Ownership and interfaces

| Module | Responsibility |
| --- | --- |
| `runtime_config.py` | Compile authorized, pinned configuration and deployment policy into an explicit startup snapshot |
| `runtime_store.py` | Reserve global process capacity and append execution events in the existing control registry |
| `runtime_process.py` | Launch a private process, inspect its birth identity and signal only its owned process group |
| `runtime.py` | Authorize start/inspect/stop, validate readiness and recover an existing runtime after manager reconstruction |
| `runtime_worker.py` | Initialize/finalize the actual RAG app lifespan and publish private identity/health observations |

The application binds `actor` to an authenticated principal. In addition to reference-use permissions, starting requires `run_runtime` and reading the batch; stopping requires `stop_runtime` and reading the batch; execution-history access requires `audit`. There is no permissive authorization fallback.

The following illustrates the internal interface, not a live deployment command. `control` is the existing authorized `BatchControl` instance with explicit roots and administrator catalogs.

```python
from pathlib import Path
from lightrag.batch_control.runtime import RuntimeSupervisor
from lightrag.batch_control.runtime_config import RuntimePolicy

policy = RuntimePolicy(
    project_directory=Path("/srv/LightRAG"),
    python=Path("/srv/LightRAG/.venv/bin/python"),
    storage_credentials_file=Path("/srv/secrets/rag-storage.env"),
    max_processes=2,
)
runtime = RuntimeSupervisor(control, policy, actor=authenticated_actor)
lease = runtime.start(batch_id, active_revision_id)
current = runtime.inspect(batch_id)
history = runtime.events(batch_id)
runtime.stop(batch_id)
```

`start` and `stop` are blocking orchestration calls. A future async HTTP adapter must run them outside its event loop and provide separate status presentation. No frontend button or public API has been added in this phase.

## Supported startup configuration

The deployment owns the absolute RAG project/interpreter paths, storage mode, credential file, process limit and deadlines. Requests cannot select these paths or executable arguments. This runtime policy is separate from the workflow checkout/interpreter catalog used for distillation.

The current adapter supports native parsing, `token`/`fixed_token` chunking and explicit OpenAI-compatible model/embedding profiles. Other parser/chunker/provider choices are rejected before launch. Each model profile supplies `model_name`, `api_url`, optionally `credential_id` plus `key_name_of_api_key`, and positive `timeout`/`max_async`. Embeddings additionally require `dimensions` and may set `batch_size`/`max_tokens`. Role references and role concurrency are compiled explicitly; unassigned roles inherit the chosen primary model. Reranking and the enterprise overlay are disabled in this adapter, and VLM processing is enabled only with an explicit VLM role assignment.

Profiles must match the published revision's snapshots. Credential values are read only from authorized server-owned dotenv references with interpolation disabled. They are passed in the child environment, never written into the startup snapshot or execution events. Database URIs and model URLs must not embed credentials. Worker logs remain private and should be treated as operational diagnostics, not published directly to users.

The compiler launches an offline probe with the selected interpreter. Parent and child compare Python version, source hashes, `uv.lock` hash and a normalized installed-distribution fingerprint. This proves agreement with the captured environment; it does not prove that every installed package satisfies the lock file. Changing a running process's effective startup policy requires an explicit stop and new generation. Recipe-only revisions can reuse the healthy RAG process; the existing generation retains its original revision snapshot.

The child receives a small environment allowlist plus compiled values. It starts in a private generation directory, so the project's working-directory `.env`, inherited workspace overrides, Python hooks and inherited model credentials do not select its configuration.

## Resource layout and readiness

All resources remain under the explicit `ControlPaths` roots:

```text
runtime_root/
  rag/<batch-namespace>/
    storage/                     # Stable across generations of this batch
    inputs/
    logs/
  batch-runtime/<batch-namespace>/generation-<n>/
    spec.json                    # Private snapshot and observation capability
    bridge.json                  # Reference to this revision's workflow YAML
    health.json                  # Atomically replaced heartbeat
    worker.log
  batch-control/locks/<batch-id>.lock
```

`runtimes` and append-only `runtime_events` are additive tables in the same `control.sqlite3` authority. They do not create another configuration authority. Initial directories are private (`0700`) and published JSON files are private (`0600`). Runtime paths reject symlink redirection. Stable storage is not deleted by stop or startup failure.

Default `neo4j_milvus` mode requires explicit Neo4j/Milvus endpoints. JSON KV/doc-status use the batch workspace; Neo4j uses its actual workspace graph label, and Milvus uses actual batch-prefixed collections. The worker reports the initialized storage bindings rather than merely echoing requested values. `local` mode uses NetworkX/Nano/JSON only for isolated development and validation; it additionally reports actual storage file paths under the batch directory.

A lease is granted only after the app lifespan has initialized storage and a fresh observation matches the reserved batch, generation, startup digest, workspace, PID, process birth identity, private capability, storage classes/scopes, actual collection bindings, embedding model/dimensions, chunk/concurrency parameters and role configuration. A stale heartbeat produces no ready lease. An invalid binding raises an integrity error.

The private file protocol carries identity and health observations only. There is no listening HTTP/Unix socket. Phase 2 alone provides no business-task command channel; the phase-3 adapter now uses the same private registry to dispatch ingestion, retrieval and distillation to the child, as documented separately.

## Capacity, failure and recovery

A transaction reserves one process slot before launching. Per-batch file locks serialize supervisor actions across manager instances, while SQLite serializes global capacity checks. `max_processes` limits managed RAG processes sharing this registry; it is not a provider-wide model concurrency or token quota. Separate registries do not share this limit.

`reserved`, `starting`, `ready`, `stopping` and `unknown` all retain capacity. The child attaches its PID/birth identity before importing or initializing the RAG so a parent crash after spawn can be recovered. A new manager reads the persistent reservation and validates the existing process and its fresh observation; it does not create a replacement merely because the original manager disappeared.

Confirmed process exit or PID reuse releases the original reservation as failed without sending a signal. Unknown ownership, a same-birth command mismatch, missing identity or an unconfirmed shutdown retains the reservation. A crash before the child attaches can therefore leave a reserved slot; automatic release is deliberately deferred because absence of a recorded PID is not proof that no child exists. Operators must verify the process state before any later administrative reconciliation; this phase supplies no force-release/delete operation.

PID observations also require an exact machine/boot/PID-namespace scope, including
confirmation that mounted procfs represents that same namespace. Reservation,
startup specification, child attachment and readiness observations bind this
scope. Foreign or unreadable scope cannot prove absence or authorize a signal;
a confirmed different boot on the same machine can prove exit without looking
up an old PID. Legacy records without scope retain uncertainty. A terminal row
with an unconfirmed previous PID cannot authorize a replacement writer. If a
launch may have begun but no PID attached, startup failure retains its slot
rather than inferring that no process was created.

Stopping records `stopping` first and sends SIGTERM only after exact command, process-group and birth-identity checks. It waits for exit before recording `stopped`. A timeout keeps the reservation held; there is no SIGKILL fallback or signal to an unrelated/reused PID. A process that cannot finish storage finalization may need operator attention. The events preserve the stopping state and generation for inspection/retry.

Startup failure cleans up only a proven owned process. If shutdown cannot be confirmed, the state becomes `unknown`. No failure deletes data or reports that an unconfirmed writer stopped. Index state remains conservatively `unknown`; runtime readiness does not prove that an index is empty or that a pending configuration/rebuild succeeded.

Pending RAG configuration applications and settings UI are implemented by the [phase-4 adapter](BatchConfigurationUI.md); task dispatch and opt-in public APIs are implemented by the phase-3 adapter. Index rebuilds, live-workspace adoption, provider admission and migrations remain outside this implementation. Existing services continue to use their existing configuration authority until explicitly migrated.

## Validation boundary

`tests/batch_control/test_runtime.py` covers persistent reservations, capacity, authorization, identity checks, false readiness, profile/policy drift, secret separation, generation recovery and independent stopping. Its local smoke launches two actual RAG subprocesses and exercises real NetworkX/Nano/JSON lifespans without issuing model requests or executing a workflow.

The [execution record](BatchControlExecution.md) links the archived observations and exact validation outcome. Neo4j/Milvus live-server isolation must be validated separately before production enablement; local lifecycle validation does not substitute for it.

## Original-page compatibility follow-up

The earlier native/fixed-token-only adapter limit above is historical. Managed
configuration now compiles paragraph semantic, vector semantic, recursive and
fixed-token defaults into the existing document router. New batches default to
paragraph semantic chunking; explicit per-document choices still win. See
[BatchWorkbenchCompatibility.md](BatchWorkbenchCompatibility.md) for the restored
UI, original integration endpoints and future-document-only application rules.

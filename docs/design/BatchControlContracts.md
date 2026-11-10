# Batch control: phase-1 contracts

This document describes the phase-1 configuration foundation for the multi-batch implementation plan. The foundation itself does not launch a RAG, mount HTTP routes or migrate the live workspace. The subsequent [phase-2 runtime supervisor](BatchRuntimeIsolation.md) provides isolated subprocess lifecycles and [phase-3 full-chain integration](BatchFullChain.md) connects headless business-task APIs. [The execution record](BatchControlExecution.md) distinguishes local validation from outstanding production acceptance.

## Modules and ownership

| Module | Responsibility |
| --- | --- |
| `lightrag/batch_control/contracts.py` | Strict configuration, batch/apply/source identities, admission and runtime-lease port types |
| `paths.py` | Explicit server-owned configuration and runtime roots, independent of recipe parent paths |
| `fields.py` | Field metadata, current override import, reusable groups and structural differences |
| `references.py` | Administrator catalogs, reference permissions and credential-free execution snapshots |
| `resources.py` | Prompt capture, hashed revision directories, verification and orphan inventory |
| `storage.py` | One SQLite authority, transactional registration, idempotency, expected-version checks and queue envelopes |
| `service.py` | Authorized create/read/preview/save/task-registration orchestration |

Constructors do not create a registry. Methods use explicitly supplied roots. Tests use temporary roots; no production `control.sqlite3` has been initialized as part of this phase. The registry schema, configuration document and manifest currently use version 1. These are independent of workflow job YAML v3, existing settings-response v2 and legacy workspace settings-file v4.

## Configuration and references

A `Configuration` has `rag`, `retrieval`, `recipe` and `bridge` groups. Unknown top-level fields are rejected. `RagConfig` v1 exposes parser selection, token chunk size/overlap, chunk strategy, insertion/native parsing parallelism, versioned model/embedding-profile references, role bindings and role concurrency. These are adapter-neutral contracts, not executable environment variables. Phase 2 must compile and validate them against the actual RAG implementation before starting a process; do not silently accept an unsupported parser/chunker/profile option.

The server supplies an immutable default document and `Catalog` of environments, profiles, credential references and input references. No configuration request chooses an executable, database/workspace name, task directory or plaintext secret. Profile snapshots and environment identifiers are recorded in each manifest. Existing reference IDs must not be reassigned to a different meaning. Changing code/profile versions requires new reference IDs. The selected workflow checkout commit is checked through its existing schema probe; dependency identifiers are recorded references, not proof that all installed packages match a lock file. Runtime environment enforcement remains a later-stage responsibility.

`describe_fields(selected_workflow_schema)` combines field schemas, Chinese labels, defaults/constraints, units, reuse and application categories; the optional selected `ModelConfig` schema supplies model-profile field details. The current recipe adapter reuses `RecipeValues` and validates editable fields/Prompt role/protocol using the selected checkout. Its offline schema probe invokes `prepare_job` with placeholder credentials and validates the current topic catalog; it does not execute a workflow or call a model.

Only `rag`, `recipe` and `bridge` can be copied. A new batch always receives an independent deep copy of deployment retrieval defaults. Copying other groups into an existing batch preserves that batch's retrieval settings. Requests to copy retrieval, workspace, collections or unrelated groups fail explicitly. Recipe defaults follow the supplied deployment document and selected current workflow schema; explicit user changes are retained.

Importing settings accepts only current v4 and retains supported actual values; legacy v2/v3 is rejected. Administrator-owned field mappings and `topic_catalog_file` belong to the target; they are not edited or imported from another batch. The selected checkout's preparation probe checks its current catalog format without generating QA. Passing preparation is not evidence that a real distillation has run.

## Authorization and Prompt capture

`BatchControl` requires an authorization callback `(actor, action, resource) -> bool`; it has no permissive production fallback. The manager must bind this callback to the authenticated principal, never a caller-supplied actor string. Actions are `create`, `read`, `edit`, `submit`, `audit`, and use of environment/profile/credential/input/retrieval references.

Copying checks source-read and target-edit permissions again during save. Only the final target's referenced resources require use permission: copying a recipe does not require using the source's unrelated private bridge credentials. Copying a bridge does require use permission for its credential/input/environment references in the target.

Direct draft Prompt paths can only reference the target workspace's managed library or shared defaults. Other batches' raw paths cannot be submitted as shortcuts. Authorized cross-batch copy reads a verified source revision, captures its real UTF-8 bytes, and publishes independent target-owned files. The existing 256-KiB/template-variable checks apply. Built-ins are captured at registration, not deferred until execution. Every copied or edited Prompt is saved with its digest; source edits do not alter target files. Preview resolves and validates in memory but publishes no configuration directory.

## Publication, apply and persistence

1. The service resolves references and validates the candidate, including source permissions and selected checkout identity.
2. It writes all configuration/Prompt files in a temporary revision directory, flushes files/directories, then renames the directory into its final location.
3. An immediate SQLite transaction checks the expected active revision and pending-operation state and registers the new revision/application record.
4. Only SQLite determines which revision is active. Directory enumeration never activates a configuration. A complete unregistered directory may remain after a concurrent conflict or failed registration; authorized `list_orphans()` reports it without deleting it.

Manifest and artifact hashes are verified on each read. Tampering fails loudly. The manifest records the environment/profile snapshots, component hashes, source groups, parent revision, actor and explicit user changes. Effective recipe hashes use Prompt content hashes rather than batch-specific paths, so copying identical contents or saving an unchanged document does not create a spurious change.

Initial registration publishes an initial configuration but leaves the batch lifecycle `registered` and index state `unprovisioned`. A workspace string is reserved in the registry; no actual graph labels, vector collections or RAG process are created. Display-name reuse does not reuse identity.

A recipe/bridge/retrieval-only change can update the default configuration atomically. A RAG startup change records `desired` and a `pending_restart` operation and leaves `active` unchanged. Phase 1 deliberately has no function that claims a real restart succeeded. The phase-4 [configuration application and settings UI](BatchConfigurationUI.md) verifies process identity, generation, resource bindings and health before completing an application. It rejects embedding changes after provisioning; existing indexes require a new batch. No automatic rebuild is implemented.

Creating, saving and queue registration accept actor-scoped idempotency keys; different payloads with the same key conflict. Expected-version changes and simultaneous applications are rejected transactionally. `operation()` and `task()` expose records only for their requested batch; there is no HTTP endpoint in this phase.

## Task and capacity ports

`register_task()` stores a `queued` envelope containing batch ID, exact configuration revision, manifest digest, Prompt digests, execution references and a typed source reference. It checks the active revision within the same transaction as registration and does not occupy an execution slot. Retrying an existing key does not switch that task to a newer configuration. Directory inputs must belong to the selected configuration. The phase-3 `ChainService` additionally checks actual retrieval publication ownership/integrity and attaches a versioned execution payload. A foundation-only envelope without that payload remains a registration record and is not dispatched. Index sources now support retrieval, center selection and query-data commands; they accept no raw paths.

`AdmissionPort.request/release/status` and the strict request/decision models define capacity semantics for RAG processes, ingestion, retrieval, distillation and model requests. A model request identifies a shared provider/credential group and may carry an estimated token count. A grant must contain a lease, and queued/rejected decisions cannot contain one. The foundation enables no scheduler or provider rate limiter. `RuntimeLease`/`RuntimePort` define the runtime boundary; the separate phase-2 supervisor verifies actual process observations and reserves process capacity in the same registry. This does not implement model-request admission or business-task execution.

## Existing bridge compatibility

`RecipeSettingsStore`, `PromptFiles`, `materialize_builtin_prompts`, `write_job_config` and `write_config_snapshot` accept optional server-owned `config_root`. Existing callers retain the previous recipe-parent default. Explicit callers keep Prompt/settings/task snapshots under the deployment root even with a deeply nested recipe path; relative resources still resolve against the recipe's own parent. No existing service call is switched to the new registry in phase 1. The legacy settings writer must be routed to the one control service when the live workspace is adopted; do not run both as active authorities.

## Validation

Run from LightRAG:

```bash
.venv/bin/python -m pytest tests/batch_control tests/integrations/data_workflow tests/api/routes/test_data_workflow_routes.py -q
.venv/bin/python -m ruff check lightrag/batch_control tests/batch_control lightrag/integrations/data_workflow/config.py lightrag/integrations/data_workflow/prompt_files.py lightrag/integrations/data_workflow/recipe_settings.py
```

Foundation tests inject a schema probe and synthetic catalogs, use real temporary SQLite/files and cover concurrent creates/applies, stale revisions, idempotency, copied resource ownership, permissions, unchanged saves, rejection of legacy imports, orphan behavior, queue snapshots and explicit roots. The selected-checkout preparation probe validates an offline plan without calling models. Phase-2 tests additionally cover real local process lifecycles as described in the execution record. These checks do not validate live Neo4j/Milvus isolation, UI, provider credentials, QA output or the full pipeline.

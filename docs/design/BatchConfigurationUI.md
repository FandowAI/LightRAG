# Batch configuration application and settings UI

This phase adds controlled application of pending RAG revisions and a batch-owned settings UI on top of [the full-chain adapter](BatchFullChain.md). It does not adopt the live default workspace, delete an index, restart an existing service or run a paid model automatically.

## Configuration ownership and editing

`BatchControl` and its registry remain the only configuration authority. The settings surface reads immutable batch revisions and publishes through that same service. It never writes legacy workspace `recipe-settings` overrides.

The UI contains RAG, retrieval, distillation, Prompt and input/execution groups. It edits supported native/fixed-token runtime settings, model-profile aliases/role concurrency, current query/evidence retrieval defaults, current workflow model parameters/stage bindings, instructions and inline Prompt content. Administrator catalogs provide selectable environment, profile, input and credential reference IDs; credential values and server file paths are not edited in the UI.

Raw workflow field mappings and topic-catalog selection remain administrator-owned and are retained from the target batch. Only `rag`, `recipe` and `bridge` groups can be copied. The source is an authorized immutable revision; copied Prompt content becomes an independent target resource when published. Retrieval settings are never copied, including when creating a new batch from reusable groups.

Inline Prompt edits are validated and captured in the new revision. They do not require arbitrary filesystem paths or a separate unversioned Prompt write. A draft preview validates current schema, roles/protocols, references and runtime support without publishing. The save revalidates and uses expected active revision and actor-scoped request identity. Retrying the same save after a lost response returns its existing result rather than applying it twice.

Recipe, bridge and retrieval changes apply to new requests/tasks without replacing the running RAG. Submitted tasks retain their original revisions. A RAG startup change sets `desired`/`pending` while retaining `active`. The UI displays both identities and requires a separate explicit application action. Changing any form value invalidates its preview. Unsaved drafts prevent batch switching; transport failures retain the save ID; conflicts retain the draft and require an explicit reload. Async reads and polling are cancelled on selection/unmount, and API-key changes remount the settings session.

## Application protocol

`applications` and `application_events` are additive tables in the existing control registry. Application states are:

```text
awaiting_action → queued → draining → stopping → starting → verifying → applied
after stop/start failure → rollback_stopping → rollback_starting → rollback_verifying
                                                    → rolled_back / rollback_failed
```

`ApplicationService` rechecks `edit`, `apply_config`, `run_runtime`, `stop_runtime` and reference permissions. Requests must identify the batch's exact pending operation and desired revision. The same request ID never automatically retries a failed attempt; a new explicit request ID creates the next attempt. Per-batch application locks serialize coordinators and user discard actions across manager processes.

Preflight verifies revision resources, administrator snapshots, supported runtime configuration and credentials before touching the old process. Existing admitted/running tasks drain normally; they are not cancelled merely to change configuration. Pending configuration closes new task admission. The old owned process is stopped with the runtime supervisor's identity checks and confirmed exit requirement.

The new generation may start provisionally against `desired` only when its reserved application ID matches the pending operation. No business task is admitted to that provisional runtime. Readiness verifies the actual workspace/storage/model/chunk/concurrency bindings through the existing lease protocol. Only a matching ready generation, checked under the runtime lock and in the registry transaction, can promote `desired` to `active` and complete the operation.

Queued tasks requiring the retired RAG component are marked `interrupted`, with their original identity/results retained; they are not silently rewritten to the new model or replayed. Application events retain the request principal, previous/desired revision, attempt, stage and final lease. The coordinator can resume after a manager restart at a durable stage; it does not infer success merely because a child was spawned.

A preflight failure leaves the original process untouched. If that original runtime can still be verified against the active RAG configuration, it is retained and admission reopens with a `rolled_back` result. A later stop/start/readiness failure first retires the owned replacement, then restarts and verifies the previous revision. The pending pointer is cleared only by a matching ready lease and configuration CAS; starting a child alone does not prove restoration. The failed revision and original error stay in history.

An uncertain stop remains `rollback_stopping` with admission closed. Failure to verify the previous runtime is `rollback_failed`; its recovery error is retained alongside the original application error. Recovery resumes at its durable phase after manager reconstruction and does not launch a duplicate child. Retry after a confirmed terminal failure requires a new explicit request identity. Discard safely reconciles any provisional different-RAG process before clearing the pending pointer. No unknown writer is force-killed or treated as stopped. The UI retains the latest application outcome even after successful rollback clears `pending`.

No embedding migration/rebuild is performed. A different embedding profile can be applied only to a batch that has never provisioned a runtime and is still `unprovisioned`; this is rechecked before stopping/starting so an ingestion/start race cannot authorize an in-place vector-space switch. Existing or unknown indexes require a new batch. All prior documents, graph/vector data and exports remain untouched by the application service.

## API

These authenticated paths extend `/data-batches`:

| Endpoint | Behavior |
| --- | --- |
| `GET /{batch}/settings` | Active/desired configuration, inline Prompts, field metadata, allowed reference IDs, permissions and pending application |
| `POST /{batch}/settings/preview` | Validate full draft plus optional group copies/inline Prompts and return differences/effect |
| `PUT /{batch}/settings` | Publish a validated draft using expected revision/request ID |
| `POST /{batch}/settings/copy-preview` | Copy authorized reusable groups into a draft without publishing |
| `GET /{batch}/applications/{operation}` | Independent application progress/status |
| `POST /{batch}/applications/{operation}/start` | HTTP 202; request controlled application or explicit retry |
| `POST /{batch}/applications/{operation}/discard` | Discard pending configuration after safe process reconciliation |
| `GET /{batch}/applications/{operation}/events` | Authorized execution history |

The existing create and configuration-save endpoints also accept optional typed `copies`; saving accepts `prompt_contents` keyed by the four current workflow stages. Copies identify `{batch, revision}` and never accept `retrieval` as a group. No request selects the execution actor or a credential value.

Application request body:

```json
{"expected_revision":"rev-DESIRED","request_id":"apply-click-001"}
```

Save/preview body:

```json
{
  "expected_revision":"rev-ACTIVE",
  "request_id":"save-click-001",
  "configuration": {"schema_version":1,"rag":{},"retrieval":{},"recipe":{},"bridge":{}},
  "prompt_contents":{"qa":"Full validated template text"}
}
```

The empty groups above are placeholders for the complete configuration returned by GET. The frontend retains administrator-owned values and sends the complete typed draft. Public settings choices omit credential paths/values. Authorization is enforced again by the backend, independently of disabled UI controls.

## UI entry and build

The regular admin WebUI has a **Batch settings / 批次设置** tab. If batch management is not enabled on its backend, this tab displays an unavailable-service message and does not fall back to a default workspace.

The standalone manager serves `/batch-settings/`, using the separate `batch-settings.html` build entry, and now also serves the full `/webui/` manager shell with its own shared login/health endpoints. It does not initialize a default-workspace RAG. The settings entry accepts a session-only API key or an existing compatible JWT and presents the same settings component. Application progress is a separate component polling its operation; a pending save is never presented as an applied configuration.

The server injects its root-path API prefix into the HTML and serves relative hashed assets under the same mount. HTML is `no-store`. Configure optional deployment `ui_directory` or CLI `--ui-directory` to point to a completed build. A missing build returns a clear 503; it does not serve a different admin entry as a fallback.

For validation or staging, build to a separate directory so the currently served assets are not overwritten:

```bash
cd /root/MasterRAG/LightRAG/lightrag_webui
/root/.bun/bin/bun install --frozen-lockfile
/root/.bun/bin/bun run build --outDir /tmp/batch-settings-webui
```

After configuring deployment paths/model references, start a new manager with its own chosen port:

```bash
cd /root/MasterRAG/LightRAG
LIGHTRAG_API_KEY="$RAG_KEY" .venv/bin/python -m lightrag.batch_control \
  --deployment /absolute/path/to/batch_control.yaml \
  --ui-directory /tmp/batch-settings-webui --host 127.0.0.1 --port "$BATCH_MANAGER_PORT"
```

This implementation does not execute that deployment command or restart current services. [The execution record](BatchControlExecution.md) contains the tested scope and archived local process/UI evidence. Live Neo4j/Milvus, external model inference, existing-workspace adoption, provider quotas and index rebuilds retain their earlier acceptance boundaries.

The subsequent [parallel and release validation](BatchParallelReleaseValidation.md) checks simultaneous workers, independent cancellation/failure and the packaged settings entry. It does not activate this deployment.

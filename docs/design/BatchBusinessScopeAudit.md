# Batch business request and view boundaries

The source/local-test boundaries below are supplemented by actual 9631
acceptance in [BatchTest9631Deployment.md](BatchTest9631Deployment.md).
The full requirement mapping remains in [BatchCompletionPlan.md](BatchCompletionPlan.md).

## Entry and request ownership

The full manager injects `batchOnly` into its normal WebUI entry, initializes no
default RAG and serves no alternate workspace HTML. Before selection,
`BatchWorkbenchContent` mounts settings rather than document/graph/query views.
The selector verifies the chosen runtime before committing the selected batch;
failed or superseded entry requests retain the previous selection. The current
batch name and physical workspace stay visible above the business views.

`batchContext.ts` binds the normal Axios business requests to
`/data-batches/{batch}/api/...`, captures a selection epoch and aborts/discards
responses from an earlier epoch. Streamed queries use the same explicit path and
cancellation context. Authentication and control-plane endpoints stay at the
manager. The selected batch never supplies an arbitrary upstream URL.

| Surface | Owned route/channel | Local evidence |
| --- | --- | --- |
| Document upload, listing, processing and cancellation | `/documents/...` through the business mailbox | HTTP bridge/full-chain backend tests; document and workbench rendered tests |
| Server-directory selection and import | `/documents/server-import/...`; task sources additionally require declared input references | Existing server-import client uses the shared Axios binding; source-drift/foreign-input full-chain tests |
| Graph labels, browsing and edits | `/graph/...` and `/graphs` through the same mailbox | Business action/admission checks; graph state reset and view remount on selection |
| Graph ingestion/entity merge settings | `/entity-merge` and `/entity-merge/preview` | Real settings SQLite with separate workspace keys; edit/paid-preview permission tests; rendered late read/save/preview tests |
| Normal, structured and streamed queries | `/query`, `/query/data`, `/query/stream` | Scoped settings/history and submitted request tests; streaming mailbox tests |
| Center selection, evidence retrieval and distillation | Frozen `/data-batches/{batch}/tasks` commands | Full-chain/parallel process tests; rendered workbench command/progress tests |
| Prompt/configuration editing | Selected batch settings and revisions | Configuration UI/preview/application tests; legacy prompt editor is not mounted for a selected batch |
| Results, logs and training downloads | Batch/task/export identities; authenticated client blobs | Cross-batch artifact rejection, pagination, late-download and publication tests |

## Private child authentication

Managed children expose no network listener. Their ASGI application uses the
generation's private capability as its API key. The mailbox strips caller
Authorization/API-key headers and inserts that capability after the manager has
checked the authenticated actor's batch grants. Internal task ASGI clients use
the same private key. This preserves routes that require configured write
authentication without copying the manager/user's credentials into a child.

Mailbox request-header names are normalized to lowercase when constructing an
ASGI scope. Otherwise a valid JSON body with `Content-Type` capitalization can
be treated as bytes and rejected by the request model. The entity-merge write
regression exercises capitalized headers, forged caller credentials, the actual
route/manager and an unchanged peer workspace.

## View lifetime and links

The main workbench remounts business views for batch/login epoch changes and
resets graph state. Query history/settings use user/batch storage keys. Task,
artifact, export and index progress components retain their explicit client and
batch scope; downloads use temporary authenticated blobs and revoke their URLs.
The entity-merge dialog also remounts on selection changes and aborts/discards
requests when closed, so reopening cannot display an obsolete save or preview.

Header brand links stay within the current HTML entry. API-documentation and
repository links are global navigation, not batch data downloads. The manager
does not expose the legacy workspace HTML entry. The ordinary server retains its
separate original-workspace mode until an explicit adoption; a missing managed
runtime never falls back to it.

The rendered StrictMode result test waits for initial async rendering before
simulating a user click. Its original immediate event failed once during a full
suite; isolated runs and a scope diagnostic did not reproduce that failure.
Those records are retained rather than claiming a proven production defect.
A deliberate broken scope initialization still fails the revised test.

## Actual acceptance boundary

Actual 9631 Firefox acceptance verifies the manager-only landing, batch identity,
batch switching, document-page entry, ingestion settings and separate task
progress without page errors. The real two-batch Neo4j/Milvus/model run verifies
ingestion, isolated original-text retrieval, center selection, evidence packages,
QA distillation, training downloads and foreign-task rejection. Real index
activation and configuration apply/rollback preserve the peer lease.

Every graph-edit, stale-response, stream and login-switch edge case is not
individually replayed in the browser: those contracts are established by the
backend and rendered component regressions, including deliberately failing
mutations. Password login was not exercised in the live browser. Historical
local tests and actual host observations are kept distinct in the final audit.
No main-service operation or main-workspace adoption is part of this acceptance.

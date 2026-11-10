# Batch selection with the original business pages

The user's follow-up replaces the expanded workbench presentation: keep batch
selection and creation in the upper left, and retain the original five business
pages and their controls. The prior chain acceptance did not establish original
UI feature parity. This record supersedes that inference.

## Page and request ownership

The main navigation contains Documents, Knowledge Graph, Batch retrieval,
Retrieval and Prompts. It no longer offers a batch-settings tab or vector-index
rebuild action. Creation uses a small name dialog beside the batch selector.
Before selecting a batch, a manager-only deployment displays a selection hint
and mounts no default-workspace business view.

Documents and graph use their original components. Batch retrieval uses the
original candidate thresholds, checkbox table, per-run depth/vector-candidate
controls, evidence-package progress and original distillation panel. Prompts
uses the original PromptManager with its extraction, gleaning, document,
keyword, graph-answer and naive-answer groups. Ordinary retrieval reuses its
original view through a wrapper that isolates settings and history by batch.

The shared client routes all these business requests to the selected owned
child. Prompt administration and the original data-work-flow integration routes
are explicitly supported. Manager/auth routes and foreign integration prefixes
remain excluded. Switching batch aborts old requests and remounts page state;
batch-retrieval restoration keys additionally include the batch identity.

## Document chunking settings

The existing Graph ingestion settings dialog includes a Text chunking section.
Paragraph semantic chunking is the default for new batches. Existing test
batches receive explicit configuration revisions with the same default.
The section exposes strategy, token size and overlap without exposing an
embedding-model change or index rebuild. Supported strategies reuse the existing
paragraph, vector semantic, recursive and fixed-token implementations.

Saving publishes a batch-owned RAG configuration and applies it through the
existing drain/readiness/rollback controller. The dialog displays pending
application and can explicitly resume an acknowledged pending publication after
closing. Late publication responses never trigger application after the dialog
or batch scope is gone. Embedding references remain unchanged.

Compiled parser rules select the configured chunker; native parsing retains a
legacy-format fallback. A managed plain-text request also inherits its batch
strategy when none is explicitly supplied. Filename/per-request chunking
choices retain precedence. Strategy-specific size/overlap values reach the
existing chunker snapshot. Already accepted and completed documents keep their
frozen settings and are not automatically reprocessed or re-embedded.

## Original workflow settings

Managed children copy their frozen recipe's Prompt defaults into an isolated,
content-addressed business configuration area. The original recipe settings and
Prompt upload editor write workspace overrides there, without changing the
immutable batch revision. Those overrides survive RAG restarts and chunking
configuration application. Each submitted workflow still captures its own
immutable task recipe/resources. The original workflow launcher participates
in shared provider admission, and active workflow jobs participate in child
activity reporting. The official data-work-flow checkout is unchanged.

## Test deployment

Only port 9631 is updated. The dedicated deployment defaults to paragraph
semantic chunking, size 2000 and overlap 100. Existing test batches receive the
same future-import settings, preserving graph/KV/vector bindings and embedding
profiles. The dedicated process capacity is 8 rather than the former 2; actual
browser creation exposed the old cap when another page occupied the second
slot. This does not change model-request quotas or start additional model
servers.

Candidate: `config/validation/original-pages-restore-20261105/release-final/`
in the parent workspace. Wheel SHA-256:
`79358f2eca5042bb1e4bbbb2bff7cb6bc2411238a542546ee762bcfa354550d1`.
The read-only launcher verification passes. Backend scope: 599 passed, exit 0;
frontend: 766 passed, TypeScript/lint/staged build passed. The pre-existing
ThemeProvider lint warning remains. A deliberate incorrect application revision
is rejected by the new rendered regression, and the original source is restored
exactly. Actual browser/API/import receipts are archived alongside these checks.

The main 9621 process and its workspace are excluded from all service operations.
No official workflow source, model profile, credential or historical vector
index is changed by this restoration.

Actual acceptance completed: the original five tabs, upper-left creation,
original batch-retrieval controls, original PromptManager and document chunking
controls are visible in Firefox with no page/API errors. The original recipe
save and Prompt draft endpoints accept writes in the new isolated test batch.
An ordinary Markdown upload completed as PROCESSED with
`chunk_method: paragraph_semantic`. Final API inspection verifies the same
2000/100 paragraph defaults in all six registered test batches, with no pending
configuration. Initial capacity/validation-script failures are retained in the
execution evidence; successful new-batch entry was checked after raising the
9631-only process cap.

The headless browser has no WebGL2 context, so this browser record does not
claim successful graph-canvas rendering. GraphViewer itself is unchanged; its
batch-owned API routing remains covered by the backend suite. No browser login
password was changed; the short-lived validation session file was removed.

## Subsequent accepted batch retrieval layout

The user subsequently approved the four-step batch retrieval redesign. Its
current behavior and module boundaries are documented in
[BatchRetrievalWorkbench.md](BatchRetrievalWorkbench.md). This changes the batch
retrieval layout and adds read-only preview/download endpoints; the remaining
original business pages and document defaults retain the contracts above.

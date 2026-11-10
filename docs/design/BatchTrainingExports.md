# Batch-owned training exports

Training exports read completed distillation evidence without calling a model,
changing the official workflow, or rewriting the original input and results.
Only completed/completed-with-errors chain or distillation tasks with a matching
terminal workflow receipt are eligible. The task's original immutable revision,
not the batch's currently desired configuration, supplies the recipe and selected
workflow interpreter. `read` and `export_training` permissions are required to
submit; every list, status and download checks batch/task ownership and `read`.

## API and progress

All paths start with `/data-batches/{batch}/tasks/{task}/training-exports`:

- `POST` accepts `request_id`, `format` (`messages`, `alpaca`, `sharegpt`) and
  `selection` (`processed`, `machine_passed`). It returns HTTP 202 and a scoped
  export record. The same request ID and parameters return the same export;
  reusing an ID with different parameters conflicts.
- `GET` lists that task's exports. `GET /{export}` reads progress/status.
- `GET /{export}/files/{artifact}` downloads `dataset`, `provenance`, `manifest`
  or the retained diagnostic `log`. Only a verified complete publication allows
  the first three files. The API never accepts a caller-supplied filesystem path.

The separate task progress module exposes exports for server-authorized terminal
tasks. Expanding the export panel loads history; an active export polls every
two seconds. Switching batch, task, client or closing the panel aborts requests
and discards late continuations, including pending blob downloads. Creation and
download errors survive unrelated successful history reads. A transport failure
retains the creation request ID so an explicit retry cannot silently duplicate
an acknowledged-but-undelivered job. Downloads use the authenticated client and
temporary browser blob URLs.

## Selection and output

The selected official `data_studio` interpreter validates manifest schema 4,
review schema 3 and the inspection contract selected by the frozen topic
catalog: `difficulty_routed_qa.v5` for the current official `CategoryCatalog`,
`difficulty_routed_qa.v6` for the ordinary `TopicCatalog`. Both are current
contracts in the pinned official checkout. The catalog type, scenario parser
(`scenarios_json.v2`) and exact inspection contract must agree; a v5 row in a
v6 topic run is still rejected. The exporter imports configuration/record
validators, not Serving, and does not translate mismatched evidence.

`processed` exports the last QA version of each processed work item. Duplicate
and technical-failure outcomes are excluded. `machine_passed` additionally
requires a completed, passed inspection whose input version is that exact last
version. A revised answer has not been inspected again; its original inspection
does not establish that its final version passed. Human review is preserved and
never inferred. A valid empty export has zero records and explicit zero sample
and exclusion counts; per-source statuses explain empty/failed input outcomes.

Every dataset row contains only the selected format:

```json
{"messages":[{"role":"user","content":"Question"},{"role":"assistant","content":"Answer"}]}
```

```json
{"instruction":"Question","input":"","output":"Answer"}
```

```json
{"conversations":[{"from":"human","value":"Question"},{"from":"gpt","value":"Answer"}]}
```

Question/answer text is preserved exactly; source evidence is not concatenated
into a new training instruction. A separate `provenance.jsonl` has one row per
dataset row, in the same order. It carries the record hash/index, stable sample
ID, batch/task/configuration revision/run identity, source/input-line and text
hashes, work-item/final-version identity and revision, retrieval submission,
original classification version, inspection evidence and actual human status.
`export.json` records identity, selection, counts, input/output hashes, workflow
fingerprint and configuration-manifest digest.

## Evidence and publication rules

Submission captures bounded hashes and physical file versions for the actual
input, authoritative task recipe/resources, run/review/events and any retrieval
source snapshot. The runtime `job.yaml` symlink must resolve to its known task
configuration; the physical authoritative file is then read without following
symlinks. Resource-content-normalized recipe identity must equal the immutable
task revision, and the official prepared configuration must equal the run's
configuration snapshot. The selected environment fingerprint must still match
the task's execution environment.

The worker verifies committed artifact sizes/byte checkpoints, every delivered
source line, original text/meta presence, unique source/QA identities, exact
source count, final-version association and matching issue/event evidence.
All captured files must remain unchanged through publication. These are export
submission snapshots, **not retroactive cryptographic proof of what existed
when the original workflow finished**. The manifest explicitly records
`source_capture: export_submission_snapshot` and `human_approval_inferred: false`.

Each export owns a private directory under
`runtime_root/tasks/{namespace}/{task}/training-exports/{export}` and an exclusive
operation lock. The worker writes and fsyncs part files, validates all evidence,
publishes both data/provenance files, then publishes `export.json` last. Only
that matching marker and its verified artifact hashes establish completion.
Missing, redirected, changed or foreign publication files prevent downloads even
when SQLite previously recorded success. A complete verified marker can recover
success after the manager missed the worker's completion.

The deployment-level `training_export_limits` in
`config/batch_control.example.yaml` bound aggregate evidence bytes, individual
rows, aggregate dataset/provenance output bytes, samples and process duration.
Exceeding a limit fails the whole publication; it never silently truncates the
dataset. Partial files and diagnostics remain available for investigation.

Queued jobs can resume after manager restart. Running jobs require confirmed
process identity/exit to become interrupted; unknown liveness is retained.
Interrupted attempts are never automatically replayed. An explicit new request
creates a new directory. A killed export is read-only with respect to its source
workflow and never signals the original distillation or another batch process.

## 2026-11-05 actual test-service acceptance

Two isolated Neo4j/Milvus test batches completed the official model-backed
workflow and Messages export, producing three samples each. The category-mode
run exposed the incorrect assumption that every current run uses v6; the
exporter now derives the exact expected contract from the frozen prepared
catalog. Three regression cases execute the official offline category workflow
and cover Messages, Alpaca and ShareGPT, while the existing cross-contract
corruption case remains rejected. No official workflow source was changed.

The first rejected export remains retained. Its successful replacement reused
the already completed paid workflow; no QA regeneration was needed. Actual
receipts and dataset/provenance downloads are archived under
`config/validation/test9631-live-resume/` in the parent workspace.

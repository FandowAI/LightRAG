# Test-port candidate deployment

This entry is restricted to the user-authorized test service on **9631** and
workspace `h200_lightrag_test_9631`. It does not operate the main service or adopt
its workspace. Target-host acceptance is recorded below and mapped to
[BatchCompletionPlan.md](BatchCompletionPlan.md).

## Candidate verification

`scripts/start_test_service_9631.py` now defaults to the accepted candidate in
`config/validation/original-pages-restore-20261105/release-final/`
under the parent workspace. `--release-directory` selects another explicitly
accepted candidate; a running service never follows that directory automatically.

From `LightRAG/`, this is a read-only check. It does not prepare configuration,
connect to a service, initialize storage, start a process or send a signal:

```bash
.venv/bin/python scripts/start_test_service_9631.py --manager-only --verify-only
```

The checker verifies the wheel hash, every installed Python/UI file, exact Python
source inventories, current source bytes, exact packaged UI inventory and the
recorded isolated smoke. Batch-manager mode additionally requires the accepted
full workbench/no-default-workspace smoke. This checks the receipt and current
artifacts; it does not repeat model/database/browser acceptance or establish
fresh-environment dependency compatibility.

## Preparation and source-service upgrade

```bash
.venv/bin/python scripts/start_test_service_9631.py --prepare-only
```

Preparation writes only the dedicated test-service configuration/resources and
loads its control configuration. It does not run the service or a model. Existing
batch configuration revisions remain authoritative. The selected UI directory is
updated in the test deployment; the prior deployment YAML is preserved under
`test-services/lightrag-9631/config-history/` before a change. Repeating unchanged
preparation does not create another backup. Both control roots must remain in the
dedicated test-service directory; configured adoption sources must name only the
9631 test workspace.

If the existing **unmanaged** test source must be upgraded to the adoption-capable
server, the normal full API/WebUI entry remains available:

```bash
.venv/bin/python scripts/start_test_service_9631.py --restart
```

The restart path first verifies local procfs/PID visibility, the 9631 listener,
test workspace environment, recognized server command, any explicit workspace/
port arguments and the actual legacy health workspace. It checks pipeline,
managed-task, workflow and test-retrieval activity before sending SIGTERM only to
those identified test listeners. Unclear ownership, a denied probe or an
unconfirmed stop prevents replacement. There is no forced-kill fallback.

The normal entry is for the source before transfer. Once storage is adopted, its
writer-owner marker prevents starting that unmanaged source again.

## Transfer and batch-manager entry

Perform the journaled test-only transfer described in
[BatchLegacyAdoption.md](BatchLegacyAdoption.md). The source declaration must use
the actual authenticated source inspection, command identity and resource paths.
An inaccessible service or an old PID file cannot supply that evidence. Do not
invent a source observation or copy the main workspace into this declaration.

The manager entry is then:

```bash
.venv/bin/python scripts/start_test_service_9631.py --manager-only
```

Before touching the port, it reads the existing registry in read-only mode and
requires an `adopted` binding for the exact test workspace, matching binding
digest and persistent physical writer-owner marker. This prevents switching to
a manager UI that silently omits the old test data. It then requires a free 9631
port. The command loads `lightrag.batch_control` from the selected installation,
passes that installation's WebUI explicitly and constructs no default RAG.

Successful startup requires both healthy APIs and proof that the newly spawned
process owns the 9631 listener. `server-start.json` records the release path,
PID/birth identity, machine/boot/namespace scope, command hash and listener proof.
It contains no credential values. Its PID must not be interpreted from another
PID namespace or a later boot.

The legacy `--restart` shortcut refuses an already-running batch manager before
sending any signal. Updating such a manager requires its owned work to be drained
and its children stopped through the scoped runtime controls before replacing
its code; the launcher does not infer that a missing default-workspace pipeline
endpoint means the manager is idle.

## Actual deployment and acceptance, 2026-11-05

The accepted wheel SHA-256 is
`79358f2eca5042bb1e4bbbb2bff7cb6bc2411238a542546ee762bcfa354550d1`.
9631 now serves that immutable candidate in batch-manager mode, listening on
`0.0.0.0:9631`; the current LAN entry is
`http://10.10.115.253:9631/webui/`. The manager constructs no default RAG.

Approved native execution resolved the earlier sandbox socket restriction.
The prior EPERM records remain historical evidence, not a statement of current
service availability. The actual legacy test service was inspected, quiesced,
backed up and transferred through the journaled adoption coordinator:

- Operation: `adoption-f5d04c5934d749e6b5eadbb8303dac44`.
- Adopted batch: `batch-623ecbddc7ee4ab8a5952d919158a904`.
- Preserved physical workspace: `h200_lightrag_test_9631`.
- The source contained zero document-status/full-document/text-chunk rows;
  this is not evidence of migrating the populated main workspace.
- The original storage bindings, test graph/vector snapshots, resources and
  recoverable adoption journal/backups were retained.

Do not restart an unmanaged full service against this adopted workspace.
Use the manager entry above after an explicit safe retirement of any current
manager and its owned children. `--restart` intentionally refuses an existing
batch manager; neither an old PID file nor an inaccessible process proves it
is safe to replace. The latest `server-start.json` records the actual release,
listener ownership, process birth and scope.

Two additional synthetic test batches exercised concurrent real extraction,
independent Neo4j/Milvus namespaces, original-text queries, center retrieval,
one evidence package per batch, official QA distillation and training export
(three QA samples per batch). The shared provider ledger records actual
cross-batch model-request overlap. A fresh four-collection Milvus rebuild
verified all 20 expected vectors before activation; the peer lease remained
unchanged. Invalid-configuration preflight retained and verified the prior
runtime; valid concurrency revisions applied and restored the original value.
The cheap center-task cancellation and explicit retry were also exercised.

Actual Firefox acceptance opened the served UI, switched both batches, opened
ingestion settings and displayed the independent task-progress module without
page errors. Browser authentication used a short-lived signed validation
session and the actual API key; a password-login flow was not exercised and
no account/password was changed. The temporary session file is removed after
acceptance. Firefox is only an acceptance tool, not a serving dependency.

Evidence is under `config/validation/test9631-live-resume/` in the parent
workspace. Failed scenario/export attempts remain alongside successful
receipts. The test-owned scenario template output was aligned to the current
typed parser through immutable configuration revisions. Neither the primary
9621 service nor its workspace was contacted or modified, and the official
`data-work-flow` checkout remains unchanged. See the final completion audit
for regression counts and the requirement-to-evidence mapping.

## Original business pages restored

The current candidate follows the user's narrower interface: upper-left batch
selection/creation with the original five business pages. Chunking settings are
in the Documents page's Graph ingestion settings dialog; defaults are paragraph
semantic, 2000 tokens and 100 overlap. The main workbench has no batch-settings
tab or embedding/index-rebuild control. The dedicated test process capacity is
8; model profiles and provider request quotas remain unchanged. Evidence and
limits are in [BatchWorkbenchCompatibility.md](BatchWorkbenchCompatibility.md).

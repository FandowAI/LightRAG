# Batch parallel execution and release validation

This phase validates the completed [full-chain adapter](BatchFullChain.md) and [configuration/settings UI](BatchConfigurationUI.md). Its result is a locally accepted candidate, not production activation. The dated outcomes and evidence locations are in [BatchControlExecution.md](BatchControlExecution.md).

## Parallel acceptance

`tests/batch_control/test_parallel_release.py` starts two actual managed RAG workers and two actual downstream CLI subprocesses. Each CLI writes its start identity and then waits for an independent release file. The test opens neither gate until both CLI processes are alive and both tasks are `running`. This proves overlap rather than inferring concurrency from two successful serial tasks.

The downstream executable is a deterministic protocol fixture, not the official QA inference implementation. RAG workers use actual local app/storage lifespans; these cases exercise directory-source distillation and make zero model requests. Native ingestion/retrieval/conversion acceptance remains covered by the earlier full-chain test; it is not claimed to run concurrently in these new cases.

Each batch has the same display name but different identity, workspace, storage namespace, source text, recipe values and captured Prompt. Concurrent repeated submissions use the same button request ID within each batch. The registry must create exactly one task per batch and the CLI must launch exactly once per task.

| Scenario for batch A | Required result for batch B |
| --- | --- |
| Complete normally | Remain running until its own gate opens, then complete |
| CLI exits 2 without a receipt | Remain running and complete with its own result |
| Cancel after both CLIs start | A's CLI is stopped; B's CLI stays alive and completes |

All cases also check:

- A reconstructed dispatcher recognizes the same two runtime leases and does not replay work.
- A third runtime cannot exceed the configured process capacity.
- Cross-batch task/artifact lookup fails, and converted input hashes differ.
- A pending RAG change on A drains its admitted task without stopping it, then replaces only A's runtime. B remains running on its original revision and generation throughout.
- Failed or cancelled work keeps its terminal state after another dispatcher tick; it is not automatically replayed.
- Both RAG workers and both CLI subprocesses are confirmed stopped at cleanup.

The overlap duration in the archive ends at the last observation that both gated CLIs were alive, before either was released or cancelled. It is not a throughput benchmark or a model-capacity measurement.

## Local release candidate

`scripts/release/validate_batch_release.py` builds and validates a wheel in a **fresh** output directory. It does not publish a package, bump a version, overwrite served assets, restart a service or install dependencies into the current environment.

The script:

1. Requires a finished WebUI build containing both `index.html` and `batch-settings.html`.
2. Copies package/build inputs into a private staging directory, excluding working `.env` files, bytecode and logs. It uses the installed setuptools PEP 517 backend without downloading a build environment.
3. Builds one wheel and compares every staged Python module and WebUI asset to the wheel's bytes using SHA-256. A missing or changed file fails acceptance.
4. Unpacks the wheel into a separate directory, then runs Python with `-I` from an unrelated working directory. The smoke verifies that the manager module came from that unpacked wheel rather than the editable checkout.
5. Constructs the standalone manager with an isolated registry and no default RAG, starts/closes its task/application/rebuild/export coordinators, checks authenticated/unauthenticated API behavior, and serves both packaged settings and full workbench HTML/JS/CSS under `/release` using ASGI. HTML must be `no-store`, the injected API prefix must match, and the workbench must declare `batchOnly`. Health must report no default workspace and the legacy workspace HTML must remain unavailable.
6. Checks the packaged `python -m lightrag.batch_control --help` contract and records the wheel hash, file hashes and smoke results in `release.json`.

The candidate keeps the checkout's current package version; it is not a new tagged release. Dependency installation/resolution in a fresh virtual environment, another Python version or another operating system is not tested by this check. The configured managed-RAG project/interpreter and pinned workflow checkout remain explicit external deployment resources; packaging the manager does not vendor the workflow environment or administrator configuration.

The phase-5 run also inspected the actual configured `data-work-flow` checkout using its selected `.venv-qa` interpreter and prepared the active recipe with placeholder credentials. Its schema version, commit, recipe digest and execution fingerprint are archived in `official-workflow-preparation.json`. This is real upstream preparation compatibility without model requests; `run_job` was not executed.

Run from `LightRAG/`, using a fresh task-specific directory for each invocation:

```bash
cd /root/MasterRAG/LightRAG/lightrag_webui
/root/.bun/bin/bun install --frozen-lockfile
/root/.bun/bin/bun test
/root/.bun/bin/bun x tsc --noEmit
/root/.bun/bin/bun run lint
/root/.bun/bin/bun run build --outDir /tmp/lightrag-release-webui

cd /root/MasterRAG/LightRAG
.venv/bin/python scripts/release/validate_batch_release.py \
  --webui-directory /tmp/lightrag-release-webui \
  --output-dir /tmp/lightrag-release-validation-new
```

The script refuses an existing output directory rather than replacing an earlier candidate. Missing legacy `requirements.txt` entries in the repository manifest may produce setuptools warnings; dependencies are declared in `pyproject.toml`.

## Acceptance boundary

The evidence supports local parallel correctness, regression checks and release-package integrity/loading. It does not establish production readiness for live Neo4j/Milvus, real official-workflow QA inference, shared-provider/token quotas, browser visual behavior, or production endpoint/reverse-proxy traffic. ASGI checks exercise the prefix and serving contract without opening a listening socket.

Before a production release, record acceptance using the chosen immutable workflow/model references and real storage endpoints in newly allocated batch namespaces, verify provider capacity and review browser behavior on the target deployment. Keep the existing workspace/service separate until its migration is explicitly executed. No index rebuild, automatic process rollback or live-workspace adoption is introduced by this validation phase.

# Shared provider admission

Managed RAG workers and managed distillation children share one provider ledger
at `runtime_root/batch-control/provider-quota.sqlite3`. A pool is identified by
the canonical OpenAI-compatible base endpoint and actual authentication value.
Different roles, model names, batches and workflow processes using that same
endpoint/key share concurrency, request-rate and token admission. Credentials,
prompts and response text are never stored in this ledger.

## Configuration and ownership

The manager deployment YAML owns `provider_limits`, separately from the
workflow recipe. `defaults` supplies `max_concurrent` (32), optional
`requests_per_minute` and `tokens_per_minute`, `default_output_tokens` (8192),
and `queue_timeout_seconds` (900). Endpoint overrides inherit omitted default
fields. Null request/token limits mean no rate cap. Endpoint policies contain
no credential values; different actual keys get separate accounting pools.
Aliases of the same canonical base URL share a policy. Different base URLs are
separate pools even when operated by one company.

Configuration validates before startup. The manager applies its declared
policies on startup without deleting counters or interrupting active requests.
Workers read the shared authority on admission; lowering a limit blocks new
grants until capacity is available. A first standalone runtime initializes the
same authority under a configuration lock. Missing/corrupt admission storage
fails closed; worker readiness requires the actual installed HTTP adapter.

## Requests, streams and unknown outcomes

Each HTTP attempt gets a durable FIFO request identity. Waiting/cancelled calls
do not count as provider attempts. A grant atomically reserves a concurrent slot
and checks the rolling request and token windows. Request-rate charges start at
grant and last 60 seconds. Token reservations remain charged throughout a live
call and for 60 seconds after it finishes. Usage returned by the provider
reconciles the reservation, including an actual value larger than the estimate.
An absent, invalid or oversized usage response retains the reservation.

The conservative input estimate uses JSON UTF-8 bytes plus framing allowance,
and an explicit output-token limit. When a token-rate cap is configured and a
generation request omits its output limit, the adapter sends the configured
`default_output_tokens` cap. Existing explicit limits are retained. Multimodal
payload estimates include their serialized content and may be substantially
larger than the provider's reported input usage. This is admission accounting,
not a promise to reproduce a provider's private tokenizer or billing window.

Async streaming calls own their slot until their response is closed. The
adapter reads bounded JSON/SSE usage without changing response content. Stream
close joins transport cleanup through cancellation before releasing admission.
The official workflow Serving is synchronous/non-streaming; each original
attempt is admitted before its existing Session executes. Redirects are
accounted as separate attempts without holding the first slot while waiting for
the next one. The existing SDK/Serving owns upstream retry policy. Local
admission rejection never starts an upstream request or adds a paid retry loop.

Elapsed time never proves that an active process/request stopped. Recovery
checks machine/boot identity, PID namespace and Linux process start ticks.
Mounted procfs must also represent the observer's PID namespace. A foreign
procfs refuses a new request owner; unreadable or foreign procfs cannot release
an existing provider slot.
Confirmed owner exit releases local HTTP concurrency; it retains token debt for
the unknown upstream result and never replays the request. A hidden process in
another namespace retains its reservation. A crash after grant but before send
can conservatively charge a call that never reached the provider. That accepted
residue expires through the normal accounting window after confirmed exit.

Queue deadlines reject waiting calls. Cancelling an async wait removes it before
send; terminating an owned workflow child is reconciled from process identity.
Owner checks are deduplicated and throttled per pool, and queue/rate lookups use
indexed records rather than repeatedly checking every historical process.

## Integration and progress

The RAG worker installs the adapter before constructing its application, covering
the SDK's actual HTTP attempts. Its health proof includes the installed registry,
batch identity and route set; the supervisor rejects a missing/mismatched proof.
Task context follows async ingestion/retrieval work and workflow subprocesses.

LightRAG's portable launcher installs the synchronous HTTP adapter and invokes
the selected `data_studio` module with the original arguments, selected checkout,
credential env-file and terminal receipt protocol. It imports only its own
portable standard-library admission core plus the selected environment's HTTP
library. No data-work-flow source, operators, configuration schema, batch
composition or retry loop is modified. Workflows without models use the original
launcher directly.

Authenticated `GET /data-batches/{batch}/provider-admission` returns the batch's
pool counts and policies. Optional `task_id` is checked against that same batch.
Task list/status responses attach queued/active model counts to running tasks.
The independent progress module displays shared-capacity waiting separately from
active HTTP requests, and clears the displayed counts on a batch switch.

Local validation uses real independent processes, the actual OpenAI SDK,
streaming test transports and the selected workflow's actual Serving class.
No external model is called by these tests. Real provider/database/browser and
deployment acceptance remain separate gates in `BatchCompletionPlan.md`.

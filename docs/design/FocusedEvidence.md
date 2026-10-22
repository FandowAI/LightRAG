# Focused evidence for batch retrieval

Production batches use two-hop graph retrieval plus thirty vector candidates,
then source-grounded topic selection and relevance-based ownership. Standalone
query/testing endpoints retain their behavior. No graph/corpus writes occur.

## Candidate scope

The batch adapter collects the center's own evidence, relationship evidence
inside the neighborhood and vector supplements. It does not unconditionally
collect every second-hop entity's unrelated lifetime evidence. Raw candidates
remain in `candidate-NNNN.json` for audit and comparison.

## Literal source selection

Unique source chunks are judged once against every selected center that
retrieved them. Paragraphs, numbered matters, headings and timestamp blocks
carry immutable Unicode-character offsets. Single newlines between numbered
matters are boundaries too, so unrelated list items are not inseparable.
Adjacent chunks from the same completed source document can supply context.
The model sees retrieval paths as hints, never as proof of topic relevance.

The rubric in `focused_evidence.FOCUS_PROMPT` is 0 unrelated, 1 generic
company/entity background, 2 necessary concrete context or decision rationale,
3 direct facts/actions/methods/decisions about the center. Only 2/3 qualifies.
Every selected topic must reference contiguous supplied units and score all
eligible centers. It must preserve reasons, conditions, negation, numbers,
time and unresolved states. The model cannot author the exported source text.

Strict JSON and source-ID validation reject malformed/truncated responses,
unknown IDs, disjoint snippets and incomplete scores. One retry is allowed.
Failed judgments and explicit uncertain/transcription-review flags remain
visible in `focus-*.json`, increase `review_pending`, and do not silently revert
to raw whole chunks. These items are excluded pending explicit review/rerun;
the batch is `completed_with_errors`, never reported as clean success.

The current GLM-5.3 selector uses low reasoning effort and a 12,288-token output
budget to reserve room for a complete JSON answer. Its thinking cannot be
disabled; see the [provider's thinking reference](https://docs.bigmodel.cn/cn/guide/capabilities/thinking).
Other configured models keep their own reasoning defaults. No private keys or
provider error bodies are placed in job status.

## Context-preserving ownership

Overlapping selections of the same original source units merge into one
coherent passage before deduplication. This keeps selected premise/conclusion
units together and prevents copying the same source span into multiple topics.
Exact/near equivalence then uses the existing scope/number/time safeguards;
unconfirmed or conflicting variants survive. Original source positions remain
in metadata. Similarity is lexical, not exhaustive semantic deduplication.

Each resulting passage belongs to the highest-scoring eligible center.
Equal scores use eligible selection-support count and canonical name as stable
tie breaks. Input processing order cannot choose the owner. Merged overlapping
selections inherit the strongest observed center score; this may broaden a
topic's context and needs human quality calibration. It is a model judgment,
not proof of optimal relevance or guaranteed preservation of every argument.

Other relevant centers receive references without repeated source text.
JSONL export and distillation submission never re-expand them. Graph relationship
and neighborhood indices are preserved in the candidate audit and filtered
structured package fields; focused teacher text
contains selected original passages and a minimal center index.

## Job lifecycle

The page shows real phase progress: retrieving centers, selecting sources,
then saving packages. A new phase starts its own count; this is not an ETA or
one combined weighted percentage. Package count increments only after atomic
file save and ledger commit. Fully shared centers are `skipped_duplicate`;
centers without qualifying topics are `filtered_out`, separate from failures.

Stopping in preparation retains candidate/selection audit files but does not
publish half-selected packages. During packaging, completed packages remain.
No crash/stop automatically repeats paid model calls. Historical batches keep
their original policy; only newly started batches use focused selection.

Explicit `POST /batch-retrieval/jobs/{id}/resume` is limited to unsubmitted
focused preparation with zero published packages. It reuses the job's frozen
candidates and only reuses successful/review audits whose full literal units,
eligible centers and validated response still match. Failed audits may retry;
published package batches cannot be silently rewritten. There is no automatic
resume on mount or after a crash. A different prompt policy must use a new job.

The selector has four bounded workers and uses the existing role wrapper,
model, credentials and cache. It never changes the shared extraction-role
concurrency limit. Operators configure provider capacity through existing role
settings; batch packaging cannot raise it while ingestion is also running.

The same long paragraph repeated by adjacent source chunk overlap is canonicalized
before topics merge, retaining both sets of literal source locations. Short
headings/replies and non-adjacent/different-document contexts do not use this
shortcut. Complete-passage exact/near matching remains a separate step.

Explicit `POST /batch-retrieval/jobs/{id}/rebuild` creates a NEW batch from an
unsubmitted terminal focused snapshot with the same centers/settings. It copies
candidate and source-judgment audits before scheduling work, leaving old output
and exports unchanged. Literal-unit/center checks gate successful audit reuse.
Failed judgments remain held during a packaging-only rebuild; it does not
silently purchase another round of model calls. Its `rebuild_of` field
distinguishes repackaging from fresh online retrieval.


Production exports retain only graph entities/edges whose source chunks support
the selected passages, plus the center. These indexes stay outside teacher text;
relationship source IDs resolve to the original full chunks in `sources.jsonl`.
Selection audits snapshot primary and adjacent original chunks before export;
source-offset reconstruction and missing-source checks fail publication closed.

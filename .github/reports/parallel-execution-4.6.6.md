# Veynuq 4.6.6 — parallel activities and evidence-based execution

Verification date: 2026-11-18.

## Concrete causes

The agent used one action_performed boolean for the entire request. Once any mutation succeeded, subsequent turns could return prose without another mandatory action. A later statement such as "now I create the corrected version" was not distinguished from a final report. Existing recovery guarded missing initial calls, unfinished plans, drafts and tool-output repetition, but did not classify repeated textual analysis or promises after partial progress.

The loop also lacked a durable, concise view of explicitly missing file targets. Model context received operation counts, while evidence about individual targets could remain in older exchanges. No loss of valid tool_calls in the parser was observed during the controlled reproduction.

The desktop bridge owned one Agent instance. Busy state, stop, pending approvals and event cursors were consequently global. Existing delegation workers had a fixed three-worker executor instead of a user-configured shared budget.

## Explicit execution phases

The task record now stores execution metadata separately from the original transcript and existing UI lifecycle states.

| Phase | Entry condition |
| --- | --- |
| ANALYZING | Preparing a new agent turn or interpreting an observation. |
| ACTION_REQUIRED | An authorized operation, missing explicit target or unfinished authorized step still needs evidence. A textual promise does not satisfy it. |
| WAITING_FOR_TOOL_CALL | The request builder is asking for the required structured action. |
| EXECUTING | The validated tool actually emits tool_start, after normal authorization and the shared-resource gate. An approval wait does not itself start an operation. |
| VERIFYING | A tool result is being checked, including backend file read-back. |
| COMPLETED | No required action, unfinished plan, unpublished write, unresolved execution failure or pending verification prevents finalization. |
| PARTIAL | Work was performed but subsequent work, verification or finalization did not complete. Uncertain outcomes retain their existing unverified UI status. |
| FAILED | The activity cannot complete and has no completed work to report. |

The ledger includes the goal, explicit unambiguous file targets still missing, pending plan steps, confirmed and failed operation metadata, the last outstanding error, file verification receipts and iteration count. Previous verified observations are presented as historical evidence requiring rechecking before reuse. The ledger is supplied alongside the model request independently of older protocol exchanges; original messages remain saved.

Text repetition uses intent categories for analysis/promises, normalized words and overlap, rather than only exact string equality. An unproductive authorized action receives at most one no-call recovery. It then fails or becomes partial with a repeated_analysis or missing-call diagnostic. Rejected promises are not saved as successful assistant actions. No prose is converted into executable commands.

After verified work, a recovery may ask for an accurate final report without forcing an additional unnecessary mutation. Explicit missing requested files or unfinished authorized plans can still require tools. File-target recognition is deliberately conservative; complex or ambiguous task meaning remains model dependent.

## File and permission behavior

File writers, atomic publication and their JSON arguments retain their existing behavior. Dispatch of shared host effects is coordinated between activities. Approval waits do not hold that effect lock. Paths, approved plugin/proposal snapshots and file versions are rechecked inside the execution gate.

Verified file receipts are checked again before final completion. A concurrent change becomes unverified instead of triggering an automatic rewrite. An uncertain file mutation is blocked for the same target, including changed arguments, until its actual state has been inspected. Existing exact-call uncertainty protection is preserved.

No permission mode is weakened. Disabled tools stay disabled. Model, quantization, context size, VRAM allocation policy and Context Manager implementation are unchanged.

## Subagents and parallel requests

- Maximum concurrent subagents: default 3, range 0–8; zero disables built-in delegation. The semaphore budget is shared by the running chats.
- The model chooses whether independent work should be delegated. Read-only investigations and coding on filtered project copies reuse existing workers.
- Batches contain up to eight tasks; the configured number determines how many run at once. Waiting workers can be cancelled without leaking a slot.
- Coding workers cannot delegate again or use host commands as a substitute for their offline execution environment. The parent reviews proposals and integrates changes through the existing conflict and approval checks.
- Maximum parallel requests: default 2, range 1–8. Each chat owns an Agent, stream, draft, follow-up queue, question, approval and stop state.
- Run in parallel opens an independent chat in the current workspace/project. Normal Send remains a follow-up to the selected running activity.
- A global event sequence combines activity events while preserving each activity's order and ownership. Approval/question identities resolve only their owning activity.
- Closing the app stops all activities. Automatic update application and configuration changes wait until all are idle.
- Scheduler completion is recorded before reusing its Agent for a manual request.
- A backend with one generation slot can queue model generations. Independent application activities do not imply simultaneous GPU generations.

## Executed checks

Before publication:

- Baseline backend suite: 240 tests, successful with the optional local Docker check skipped.
- Additional regression suite: 115 tests, all passed.
- Existing UI suite, context/task UI checks and new parallel UI checks passed.
- Python compilation, JavaScript syntax and whitespace checks passed.
- Visual inspection used an isolated Edge renderer with a fake backend, at 1440×980 and 880×720. The screenshots contain English demo data and no mouse pointer. Two existing README images were replaced; the existing optional-tool image remains the third illustration.
- The immutable restored UI baseline receives a narrow patch changing its old "Return to active chat" expectation into an enabled independent composer.

New regression coverage includes independent simultaneous requests, targeted cancellation, per-chat text/drafts, global request capacity, approval/question ownership, shared worker capacity, cancellation of waiting workers, host-effect serialization, revalidation after waiting for execution, scheduler result ownership, saved limits, delegation disabled at zero and model-selected delegation.

State-machine checks include a real file write and read-back, promise-only replies, semantically repeated analysis, recovery to a real write, missing files after partial progress, failure, duplicate verified writes, truncation, step limits, unverified writes, changed-argument uncertainty, concurrent file changes, continuation after failure and evidence surviving simulated model-input compaction.

## Actual local model integration

Model: hf.co/mradermacher/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic-GGUF:Q8_0.

Tests used a disposable profile/project and innocuous text files, with online tools and host commands disabled.

| Scenario | Result |
| --- | --- |
| Crea un file saluto.txt che contenga esattamente Ciao mondo. | COMPLETED in 2 outer iterations; exact bytes Ciao mondo.; write_file and backend SHA-256 read-back succeeded. |
| Two concurrently started requests for a.txt and b.txt | Both activities overlapped and completed in 2 iterations each; exact single-byte A and B contents. |
| Two independent read-only subagents, then parent integration | Parent completed in 3 iterations; both workers completed with actual reads, and the parent combined their observations. |
| Named echo_test selection directly to the local worker | finish_reason tool_calls; valid name and exact JSON arguments. The echo call was not dispatched to any host tool. |

These are outer agent iterations, not an assertion that every underlying transport/JSON recovery uses one generation. Earlier direct auto/required compatibility checks remain documented in tool-choice-4.6.5.md. The new named-tool probe also passed. No custom Jinja template was imposed.

## Main changed files

- veyq/parallel.py: activity coordination and shared resource/worker limits.
- veyq/engine.py and veyq/progress.py: execution phases, textual stalling, evidence ledger, explicit missing targets, uncertain mutations and final read-back.
- veyq/desktop.py and veyq/storage.py: session-scoped API, saved settings and request ownership.
- veyq/workflows.py and veyq/proposals.py: configurable bounded subagents and worker lifecycle events.
- veyq/tools.py and veyq/tool_routing.py: relevant delegation availability, normal gates and coordinated dispatch.
- veyq/scheduling.py: preserve scheduled task results when activities overlap.
- app.js, app.css, index.html and i18n.js: parallel composer, independent drafts/streams, translated limits and activity state.
- README, two screenshots, release version/build files and .github verification files.

## Residual limits

Intent classification and extraction of explicit file targets are conservative heuristics, not a general semantic proof of arbitrary natural-language goals. The model can still choose a poor plan or return invalid/incomplete output. Recovery is bounded and verified partial results remain visible. Coding-worker changes still require parent review/integration, and GPU generation concurrency depends on the configured server rather than being silently increased.

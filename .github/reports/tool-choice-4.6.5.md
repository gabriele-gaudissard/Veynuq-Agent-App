# Tool selection and progress verification — 4.6.5

Date: 2026-11-18. Tests used a temporary profile and temporary project, not personal files.

## Findings

- The configured local path used Ollama /api/chat. It had no supported tool_choice field. A harmless conflicting-instruction probe with required returned ordinary READY text through both /api/chat and Ollama's /v1 wrapper. Adding a field to those payloads alone was insufficient.
- The actual Windows worker is llama-server 0.9.0-dev, build 1, commit 6c01da627. Its help reports Jinja enabled by default. Its properties contain the GGUF chat template. No replacement template or model was introduced.
- All 49 enabled production definitions were previously advertised on every turn. File requests now receive 13 relevant definitions in the integrated fixture. Tool scope remains operational after the first successful mutation, so subsequent creation/editing tools do not disappear.
- The old repeated-action counter reset to zero after every successful read. Thus repeated successful reads of identical files could continue indefinitely with no task progress.
- Actual failing fixture calls used edit_file old="1: old" and new="1: new", copying the reader's display labels. A fallback write then saved the incorrect "1: new" literal. This was observed, not inferred. The model-facing copy now presents the actual excerpt as content and start_line separately. Original saved/UI results remain unchanged. Neither file-tool implementations nor schemas were changed.
- Before the literal-excerpt correction, repeated integrated trials were inconsistent: one completed correctly; others exhausted their configured iterations or stopped for no progress with incorrect content. After the correction, two consecutive identical integrated trials completed all five operations with exact expected bytes.

The cited upstream issue describes a related long-prompt/many-tool symptom, with a later ROCm/Vulkan distinction. The local GPU is NVIDIA, and auto succeeded in the controlled echo matrices; that issue is not established as the sole cause here.

Source: https://github.com/ggml-org/llama.cpp/issues/26530

## Request policy

- Explanations, displayed code and ordinary conversation: none, with no definitions.
- Optional resource inspection: auto.
- Authorized action still requiring execution: required on a supported compatible endpoint or the verified owned local worker.
- After the first successful action: auto with the remaining task's relevant capabilities preserved.
- Short resume requests keep the prior task's domains in auto mode.
- At most one no-action recovery. Invalid JSON/transport recovery retains its separate existing bounds.
- Known enabled tools requested outside the initial subset trigger regeneration with the relevant definition added, before dispatch. Disabled tools are never restored.
- The native fallback does not claim required selection was enforced.

Worker binding checks the configured API's listener, the worker's parent, executable location, exact loaded model path, loopback listener and /props. No port scan, alternate server launch, custom template, authentication bypass or permission change is used. Native loading retains the configured context. A local heartbeat maintains the scheduler lease while the activity is alive.

Logs record adapter, requested choice, whether selection can be enforced, definition count, finish reason, call count, text length and recovery/progress phases. File contents and complete prompts are not added to diagnostics.

## Echo matrix

echo_test was a synthetic, harmless definition. Calls were validated, never dispatched to a file or command tool. The 50-definition case consists of the 49 production definitions plus echo_test.

| Case | Definitions | Direct worker | Through ModelClient |
| --- | ---: | --- | --- |
| auto, single tool | 1 | valid echo, tool_calls | valid echo |
| required, single tool | 1 | valid echo, tool_calls | valid echo |
| auto, full catalog | 50 | valid echo, tool_calls | valid echo |
| required, relevant subset | 4 | valid echo, tool_calls | valid echo |

The final comparison used the same 100-line background and identical echo instruction, temperature 0.15, thinking disabled and 1,024 output tokens. Direct measured generation/request times were 0.70, 0.47, 1.42 and 0.75 seconds, respectively; these are one-run observations, not benchmarks. ModelClient's initial discovery adds latency.

An earlier direct-worker stress matrix also passed all four cases with 650 background lines; the full catalog prompt used 11,671 reported input tokens. A ModelClient trial with a larger background was correctly rejected by the existing conservative context preflight before generation; context logic was not changed to bypass it.

A contradictory probe ("reply READY without tools" while required was set) reached its output limit without a complete call, even at 1,024 tokens. This is not an application request policy: normal conversation now uses none. It demonstrates that required is not a guarantee of a usable completed model response. Existing truncation validation prevents dispatch.

## Integrated and automated checks

Final fixture request: read input.txt containing old, change it to new, create proof.txt containing verified, then read both files. ToolRunner was restricted to file/plan/checkpoint operations within the disposable project.

Two consecutive final runs:
- read_file → edit_file → write_file → read_file → read_file.
- Both exact byte checks passed.
- Both ended completed, with five completed operations, no failed operations and no pending verification.
- Elapsed times: 9.14 and 9.54 seconds.
- required was used before the first mutation; auto afterward. Thirteen definitions were maintained.

Automatic suites before publication:
- 240 baseline backend tests: passed, with the optional local Docker test skipped.
- 87 additional backend regression tests: passed.
- Existing UI suite: passed.
- Context/settings/task UI checks, including a zero step limit and removal of the old maximum input attribute: passed.
- Compilation, JavaScript syntax and git diff whitespace checks: passed.
- The pinned-baseline CI patch was applied independently in a disposable directory.

New checks include selection modes, file/code explanation distinctions, domain selection, later-step tool retention, excluded-tool preservation, literal excerpts retaining original numbered file text, short resume scope, transport pairing, unsupported choices, native fallback, worker identity/model checks, bounded repeated-read recovery, changed ranges/results, repeated file states, controlled resumption to a verified edit, persistence of 0 and 5,000 steps, and cancellation after 1,002 turns.

## Step limits and residual limits

Zero now genuinely means unlimited turns and disables the derived total-call budget. Positive whole numbers are finite limits without the previous 1,000-turn ceiling. Stop, approvals, schema validation, finite recovery and no-progress detection remain active.

The no-progress guard fingerprints observations without logging their content. Four repeated unchanged observations trigger guidance; repetition continuing through another such group stops the activity with no_progress. Distinct inputs/ranges/results remain eligible. Rewriting a previously known file state is not treated as new progress merely because its backup ID changed.

The selected model and quantization remain unchanged. Required tool choice cannot ensure correct reasoning or completion for every request. Future backend changes can make a private local worker unavailable; the native fallback retains its supported behavior and records the limitation. This release does not assert universal model correctness or identity with another agent.


## Release-gate follow-up

The initial fresh-install CI attempt timed out. The existing harness captured the complete installer output in pipes, hiding installation progress; child-process pipes also delayed timeout cleanup. The gate now streams the installer's existing output directly to CI while retaining its 1,200-second limit. This is a verification-harness correction, not a change to the application installer. The release remains gated by the real fresh-install result.

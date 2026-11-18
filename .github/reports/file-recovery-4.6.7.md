# Veynuq 4.6.7 — precise file conflicts and bounded recovery

Verification date: 2026-11-18.

## Confirmed causes

The old edit_file implementation merged zero and multiple literal matches into one ValueError. It could not identify candidate locations or bind a disambiguating line range to a known file version. The old interface deliberately refused ambiguous replacements; this safety behavior was correct and remains enforced.

After a failure, the agent had no activity-local memory of the failed patch identity or previously authorized read result. Its general observation guard compared full arguments and results: changed checkpoint wording, alternating observations or an intervening error could look like new progress. Successful unchanged reads still invoked the reader repeatedly. There was no targeted limit on distinct attempts to repair a failed patch.

write_file correctly refused an existing destination without overwrite permission, but returned a prose error without a machine-readable conflict or a specific recovery path. It did not distinguish a requested new version from replacement, or require a separate conflict confirmation for ambiguous model-supplied overwrite=true in full access.

## Localized corrections

- veyq/editing.py: TEXT_NOT_FOUND and AMBIGUOUS_MATCH diagnostics, overlapping-match counting, bounded candidate ranges and file hash. Optional start_line/end_line must be accompanied by the current expected_sha256. Unique surrounding literal text remains supported through the original path/old/new interface.
- veyq/tools.py: exact edit preview and execution share one plan. Existing atomic staging, backup and external-change checks are reused. The reader, checkpoint implementation and other tool implementations are unchanged.
- veyq/editing.py and engine.py: activity-local authorized read reuse, content-version checking, Windows path normalization, duplicate failed edit/write rejection and at most two different recovery attempts after the initial failed operation. Recovery asks for a more precise next operation rather than restarting the full analysis.
- Checkpoints without new tool evidence are rejected even when their progress wording varies. Two rejected redundant checkpoint requests stop the activity; a new actual observation/action or human follow-up can justify a later checkpoint. Calls after an exhausted recovery in the same batch are not dispatched, and every call still gets its paired result.
- Existing write destinations return DESTINATION_EXISTS, the known version and a suggested available filename. A clear human replacement directive naming the target permits explicit overwrite=true, subject to the existing selected approval policy. Ambiguous replacement uses the normal approval preview even in full access. Negative instructions are preserved. final=true never authorizes overwrite.
- A requested new version selects an available _v2, _v3, … destination. Multipart writes latch that destination until publication. The agent verifies and records the actual path. A confirmed repeated publication can reuse its receipt instead of creating another duplicate version.
- veyq/i18n_backend.py: distinct English, Italian, Spanish and French terminal diagnostics for edit repetition, exhausted edit recovery, checkpoint repetition and unresolved write conflicts.
- README.md, installer/Veynuq.iss, version/build metadata and release workflow: documentation and 4.6.7 packaging. index.html changes only the displayed version.
- .github/tests/test_file_recovery.py: 36 new deterministic temporary-file tests. Two older regression prompts now explicitly authorize the overwrite those tests expect; separate new tests verify ambiguous confirmation and denial.

No model, server launch, parser, Context Manager, context size, token budget, quantization, other tool implementation or permission policy was changed. No real Arduino sketch was read or modified.

## Executed local checks

- Baseline backend suite: 240 tests, successful with the optional local Docker test skipped.
- Complete regression suite: 151 tests passed after the final changes, including all 36 new focused cases.
- Python compilation and JavaScript syntax checks passed.
- Existing UI tests, context indicator/settings tests and parallel-chat tests passed.
- git diff --check passed.

The GitHub workflow reruns the complete 240-test baseline and all 151 current regression tests, UI checks, actual Linux offline-container checks, fresh installation, native installer checks and signed update/EXE publication. Its final status is checked before reporting publication complete.

## Reproducible outer agent iterations

| Controlled scenario | Iterations | Observed result |
| --- | --- | --- |
| Read → ambiguous edit → verified line-scoped edit → final response | 4 | Correct second match changed, backup retained, hash verified, completed. |
| Read → failed edit → repeated unchanged reads | 5 | Physical reader invoked once; stopped with edit_no_progress. |
| Initial failed edit and two different failed repairs | 3 | Original intact; stopped with edit_recovery_exhausted. |
| Identical failed edit or write repeated | 4 | Original intact; no repeated mutation dispatched, targeted stop also survives the existing generic repeat limit. |
| Changing checkpoint wording without new evidence | 3 | Only initial checkpoint saved; checkpoint_no_progress. |
| Existing file → explicitly authorized replacement → final response | 3 | Replacement backed up and verified, completed. |
| Requested new version → final response | 2 | Original retained, available alternative published and verified, completed. |
| Read → existing-destination failure → unchanged rereads | 5 | Original intact, write_conflict_no_progress, no false completion. |

Additional checks cover zero/one/three and overlapping matches, CRLF, stale/missing range hashes, legitimate new read ranges, clipped large read results, changed-file rereading during an active task, duplicate receipts, ordered staged new versions, occupied alternative names, external changes during staging/backup/approval, backup failure, explicit no-overwrite instructions, ambiguous denied/accepted replacement, invalid arguments including invalid Unicode and same-batch stopping.

## Limits

The current model was not changed or used as the test oracle for these deterministic cases. Model replies are simulated; actual tools execute against disposable text files and real filesystem verification. Natural-language replacement recognition is intentionally conservative: uncertain phrasing asks for confirmation. A model can still fail to produce a useful corrected call; the activity then stops with accurate failed/partial/unverified evidence.

Existing filesystem publication checks detect external changes during preparation and immediately before publication. They do not promise an operating-system transaction against a hostile concurrent process changing the same file between the final check and replacement. This release preserves the existing atomic publication mechanism rather than adding unrelated filesystem infrastructure.

No visual redesign or new screenshot was needed for these backend-only fixes.

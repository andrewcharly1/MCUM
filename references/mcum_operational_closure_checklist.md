# MCUM Operational Closure Checklist

Use this checklist before any final response for MCUM-governed work.

## Required Gates

- [ ] Task scope is identified and tied to a project path.
- [ ] MCUM is the outer orchestrator.
- [ ] Worker skill/tool path is named or inferable from the log.
- [ ] Source files, pauta, instructions, tests, or acceptance criteria were reviewed.
- [ ] Artifacts were created or modified only in the intended scope.
- [ ] Validation evidence exists and is specific.
- [ ] MCUM record step was executed.
- [ ] Final answer includes `mcum_session_id` when record succeeded.

## If MCUM Record Fails

- [ ] Retry once with the lightest available record path.
- [ ] Preserve artifact paths in the final response.
- [ ] State the blocker exactly.
- [ ] Do not mark the task as fully complete.

## Forbidden Closures

- Do not say "listo", "terminado", "completado" or "validado" if MCUM did not record.
- Do not omit MCUM failure details when artifacts were generated.
- Do not let correction-only or review-only tasks skip MCUM.

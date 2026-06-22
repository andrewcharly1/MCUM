# MCUM Command Execution Contract

Use this checklist before executing commands for tasks inside `the workspace`.

## Required MCUM Tool Shapes

- Intake: call `mcum_prepare_intake` with `task_type` in Spanish and `execution_mode` in Spanish.
- Managed command: call `mcum_run_managed_command` with `execution_mode="ejecutar"` for actual command execution.
- Validation-only command: still use `execution_mode="ejecutar"` if a shell command runs; describe the intent in `task_type="validar"`.
- Final record: call `mcum_record_task_result` with artifact paths and validation evidence.

Allowed `task_type` values:

- `analizar`
- `crear`
- `corregir`
- `mejorar`
- `planificar`
- `validar`
- `automatizar`

Allowed `execution_mode` values:

- `analizar`
- `proponer`
- `ejecutar`

## Windows Path Rules

- Prefer PowerShell `-LiteralPath` for real user paths.
- Avoid manually retyping accented filenames when Python can discover them with `Path.glob`.
- Avoid treating console `?` output as file corruption until the document content is checked directly.
- Keep shell logic in one shell; do not enumerate in PowerShell and delete/move in another shell.
- If an Office document is locked, write a new output filename or `FINAL_SUBIR` copy instead of overwriting.

## Artifact Validation Rules

- DOCX: inspect paragraphs and tables; check required headings, source data, and absence of mojibake.
- XLSX: open with `openpyxl`; check formulas exist where formulas are required.
- PDF: verify export exists and file size is non-zero.
- Rubric tasks: run the final rubric/specification checklist after artifacts exist, not only before drafting.

## Known Failure Fixes

- Symptom: `invalid choice: managed_command`.
  Fix: use `execution_mode="ejecutar"`.
- Symptom: Python path contains `?` for an accented filename.
  Fix: discover the file with `Path.glob("*.xlsx")` or pass the path as a proper Unicode `Path`.
- Symptom: `doc.save(...)` fails on an existing DOCX.
  Fix: assume Word/OneDrive lock, generate a new filename or copy into `FINAL_SUBIR`.
- Symptom: validation says accented text is missing but document internals show `\xe9`, `\xf3`.
  Fix: treat it as console encoding display noise and validate via document internals.
- Symptom: worker exits with code `0` but did not analyze the workbook.
  Fix: inspect the worker payload/artifact, not only the process exit code.

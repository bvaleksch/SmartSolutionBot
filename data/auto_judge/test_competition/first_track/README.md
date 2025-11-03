# Auto-Judge Assets · `test_competition` → `first_track`

This directory contains the reference data and rules used by the automatic judging
pipeline for the `first_track` of `test_competition`.

## Evaluation Workflow

1. The contestant uploads a ZIP archive containing their solver.
2. The service extracts the archive to a temporary directory.
   - The archive **must** include `main.py` in its root (archives wrapped in a single folder are flattened automatically).
   - The script is executed inside a Docker container (`python:3.11-slim`) with the command
     `python3 main.py input.csv`.
3. The container must produce `output.csv` in the root directory.
4. The evaluator compares the generated results with the reference `input.csv` stored in this folder.
   - Expected behaviour: square the `num` column and echo it back with the same `id`.
   - Score = number of correct rows + random bonus in `[0, 1)`.

If any of the required files are missing, or Docker fails to run the script, the scorer
returns an error result and the submission remains in an error state.

## Files

- `input.csv` — canonical dataset supplied to every submission.
- `README.md` — you are reading it.

Additional resources (checkers, extended tests, etc.) can be placed alongside this file.

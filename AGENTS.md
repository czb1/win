# Repository development constraints

- Use the repository's `.venv` (Python 3.11) for local runs and tests.
- Do not add dependencies in future changes. Application code must use only Python's standard library or libraries already listed in `requirements.txt`.
- Do not install additional packages into `.venv` or alter `requirements.txt` to introduce new libraries.
- Any application code, game configuration, or strategy change must update at least one of `docs/战斗.md`, `docs/经济.md`, `docs/防御.md`, or `docs/自进化.md` in the same change. Tests, CI, and developer tooling alone do not require a game-design document update.
- Do not create additional design documents. Append the strategy, setting, reason, lesson learned, and verification to the relevant module document's change log.

## Validation budget

- Use `.venv/bin/python tools/run_checks.py --quick` during iteration. It explicitly omits marked simulation tests and is not final acceptance.
- Run `.venv/bin/python tools/run_checks.py` once on the final code. It retains every unittest (including mirrored first-day construction, three-day rebuilding and five-day upgrades) plus the independent strategy benchmark. CI uses this full mode too.
- Read the short summary first. Full output is in `artifacts/validation/full.log` (or `quick.log`); inspect only relevant failure sections. Do not paste entire traces or historical benchmark JSON into the conversation.
- Do not immediately rerun standalone construction/progression/day-economy benchmarks already covered by the passing full suite. Run extra comparisons only for a concrete unresolved strategy risk; use the relevant base revision and identical scenarios, save raw output under `artifacts/`, and report metric differences.
- If code changes after validation, rerun the affected checks and the final full check. Do not reuse prior results as proof for changed code. Do not remove assertions or shorten final multi-day scenarios to save tokens.

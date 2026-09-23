# Repository development constraints

- Use the repository's `.venv` (Python 3.11) for local runs and tests.
- Do not add dependencies in future changes. Application code must use only Python's standard library or libraries already listed in `requirements.txt`.
- Do not install additional packages into `.venv` or alter `requirements.txt` to introduce new libraries.
- Any application code, game configuration, or strategy change must update at least one of `docs/战斗.md`, `docs/经济.md`, `docs/防御.md`, or `docs/自进化.md` in the same change. Tests, CI, and developer tooling alone do not require a game-design document update.
- Do not create additional design documents. Append the strategy, setting, reason, lesson learned, and verification to the relevant module document's change log.

## Validation budget

- Run `.venv/bin/python tools/run_checks.py` for iteration and final delivery. The default is fast checks only; `--quick` remains a compatible alias. CI uses this same fast mode.
- Complex simulations and the independent strategy benchmark are disabled by default. Do not run them automatically for routine development or final delivery.
- Only run `.venv/bin/python tools/run_checks.py --full` when the user explicitly requests full simulation validation or opts in to investigating a concrete simulation regression. Existing assertions and scenarios remain intact.
- Read the short summary first. Logs are in `artifacts/validation/quick.log` (or `full.log` for explicit full mode); inspect only relevant failure sections.
- Do not run standalone construction/progression/day-economy benchmarks or historical comparisons by default. Avoid raw unittest discovery for routine validation because it includes simulations; use the unified runner.
- Mark new simulation tests with `@replay_test` so fast validation excludes them. Keep focused protocol, command legality, state transition and boundary-condition checks in the default suite.
- If code changes after validation, rerun the affected checks and the default fast suite. Report omitted simulation coverage honestly; fast success does not establish multi-day strategy performance.

# Repository development constraints

- Use the repository's `.venv` (Python 3.11) for local runs and tests.
- Do not add dependencies in future changes. Application code must use only Python's standard library or libraries already listed in `requirements.txt`.
- Do not install additional packages into `.venv` or alter `requirements.txt` to introduce new libraries.
- Any application code, game configuration, or strategy change must update at least one of `2.0版本设计文档/战斗.md`, `2.0版本设计文档/经济.md`, `2.0版本设计文档/防御.md`, or `2.0版本设计文档/自进化.md` in the same change. Tests, CI, and developer tooling alone do not require a game-design document update.
- Do not create additional module design documents. Append the strategy, setting, reason, lesson learned, and verification to the relevant current-version module document's change log. Directory `README.md` indexes are allowed.

## Competition documents and design versions

- The current stage is v2.0 / 32进16. Read `比赛文档/v2.0-32进16/README.md`, then the v2.0 task book and interface specification before designing or changing behavior. The change summary and samples supplement those specifications; unresolved contradictions must be recorded rather than guessed.
- Maintain current design decisions and verification in `2.0版本设计文档/`. Its initial documents describe requirements and pending adaptation; they do not establish implementation or match validation.
- `比赛文档/v1.0-初赛/` and `1.0版本设计文档/` are historical archives. Use them for traceability and review of inherited behavior, not as current competition rules or evidence of v2.0 correctness.
- Preserve archived source documents, samples and test records. Do not copy historical test results into the current version as new verification. `examples/` remains the existing v1.0-derived replay/test fixture directory.


## Validation budget

- Run `.venv/bin/python tools/run_checks.py` for iteration and final delivery. The default is fast checks only; `--quick` remains a compatible alias. CI uses this same fast mode.
- Complex simulations and the independent strategy benchmark are disabled by default. Do not run them automatically for routine development or final delivery.
- Only run `.venv/bin/python tools/run_checks.py --full` when the user explicitly requests full simulation validation or opts in to investigating a concrete simulation regression. Existing assertions and scenarios remain intact.
- Read the short summary first. Logs are in `artifacts/validation/quick.log` (or `full.log` for explicit full mode); inspect only relevant failure sections.
- Do not run standalone construction/progression/day-economy benchmarks or historical comparisons by default. Avoid raw unittest discovery for routine validation because it includes simulations; use the unified runner.
- Mark new simulation tests with `@replay_test` so fast validation excludes them. Keep focused protocol, command legality, state transition and boundary-condition checks in the default suite.
- If code changes after validation, rerun the affected checks and the default fast suite. Report omitted simulation coverage honestly; fast success does not establish multi-day strategy performance.

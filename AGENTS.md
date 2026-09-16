# Repository development constraints

- Use the repository's `.venv` (Python 3.11) for local runs and tests.
- Do not add dependencies in future changes. Application code must use only Python's standard library or libraries already listed in `requirements.txt`.
- Do not install additional packages into `.venv` or alter `requirements.txt` to introduce new libraries.
- Any code, configuration, or strategy change must update at least one of `docs/战斗.md`, `docs/经济.md`, `docs/防御.md`, or `docs/自进化.md` in the same change.
- Do not create additional design documents. Append the strategy, setting, reason, lesson learned, and verification to the relevant module document's change log.

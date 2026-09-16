# Repository development constraints

- Use the repository's `.venv` (Python 3.11) for local runs and tests.
- Do not add dependencies in future changes. Application code must use only Python's standard library or libraries already listed in `requirements.txt`.
- Do not install additional packages into `.venv` or alter `requirements.txt` to introduce new libraries.

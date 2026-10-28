# Rules for coding agents (Codex, Antigravity, Gemini)

- The spec is `docs/SPEC.md`. Follow it literally. Where it is silent, pick the simplest option and leave a `# DECISION:` comment.
- Do ONLY the phase the prompt asks for. Do not start the next phase.
- Python 3.12 via `uv` (`uv python install 3.12`, `uv venv --python 3.12`). Pin versions from SPEC "Stack" table.
- Copy the spec's reference code (money.py, allocation.py, simplify.py, SQL) exactly; do not "improve" it.
- Non-negotiables in SPEC "Instructions for the coding agent" always apply (integer paise, append-only ledger, Postgres owns correctness, no innerHTML).
- Before finishing: `ruff check . && ruff format --check . && pytest` must pass. Never skip, xfail or delete a failing test to make it green.
- Commit at the end of the phase on branch `main` with message `phase N: <what>`. Do not push.
- Finish with a short report: files added, test count passed, every `# DECISION:` you made, anything you could not do.
- Local ports: 8000, 8100, 3000, 9090 are used by other projects on this PC. SplitSync dev uses app 8300, Postgres 5433, Redis 6380. Compose project name `splitsync`. Never stop or touch containers of other projects (hopper, sift). If `docker` can't see containers, run `docker context ls` and use `DOCKER_CONTEXT=default`.

# AI usage notes

SplitSync was built with AI coding agents, on purpose and in the open. This page says who did what, so the work can
be judged honestly.

## How the work was split

- **The spec** ([SPEC.md](SPEC.md)) and the [Master Guide](MASTER_GUIDE.md) were written first. They fix the data
  model, the SQL, the reference code for money, allocation and the solvers, and every number the demo must show.
- **Claude** acted as the manager. It turned the spec into one prompt per build phase, then checked each phase
  itself before writing the next prompt: it read the diff, ran the tests several times, compared the reference code
  and the migrated schema with the spec, and drove the app in a browser. Every bug it found went back as a
  follow-up prompt (phases 7b and 8b).
- **ChatGPT Codex and Antigravity** wrote the application code and tests for phases 1 to 8, one phase per prompt.
  Which one wrote a phase depended on usage limits; Antigravity took over when Codex's quota ran out.
- **Claude wrote directly** where an agent's quota ran out mid-phase or the work was operational: the end of the
  frontend redesign (phase 7c), the CI/CD workflows, `deploy/deploy.sh`, the EC2 host setup and the smoke test, and
  the [UML diagrams](diagrams.md) and their renderer.
- **Antigravity drafted** [design.md](design.md), the [ADRs](adr/), [demo.md](demo.md) and
  [benchmarks.md](benchmarks.md) from the spec, the guide and the code. Claude then checked every claim against the
  code and corrected about forty: invented measurements, wrong button labels, a wrong lock key, wrong limits.
- **Images:** the README banner is drawn from the demo trip's real numbers (10 IOUs, 3 transfers), not generated.

## What the owner did

Chose the project and its scope, ran every agent prompt, tested the app by hand on localhost before any deploy,
decided the hosting (a new EC2 instance, HTTPS on a free sslip.io name), and logged in to AWS and GitHub for the
deploy. The owner is responsible for the result and works through the
[Master Guide's](MASTER_GUIDE.md#know-your-code-checklist) know-your-code checklist to be able to explain every part
without notes.

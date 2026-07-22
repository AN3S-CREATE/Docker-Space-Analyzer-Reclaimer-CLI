# AGENTS.md — Instructions for AI agents & contributors

> **READ THIS FIRST.** This project is mirrored across **three GitHub
> repositories that MUST be kept byte-for-byte identical.** Every change has to
> land on all three. Do not treat any one of them as canonical over the others.

## The three mirrors

| Purpose | Remote name | URL |
| --- | --- | --- |
| VeralogixCatalyst | `origin` | https://github.com/VeralogixCatalyst/Docker-Space-Analyzer-Reclaimer-CLI.git |
| AN3S-CREATE | `an3s-create` | https://github.com/AN3S-CREATE/Docker-Space-Analyzer-Reclaimer-CLI.git |
| veralogix-group-innovation | `veralogix-group` | https://github.com/veralogix-group-innovation/Docker-Space-Analyzer-Reclaimer-CLI.git |

## Golden rule: keep all three identical

Whenever you commit, you **must push the same commit(s) to all three remotes
individually.** A change is not "done" until `main` on all three points at the
same commit SHA.

## First-time setup (configure the remotes)

If a clone only has `origin`, add the other two:

```bash
git remote add an3s-create      https://github.com/AN3S-CREATE/Docker-Space-Analyzer-Reclaimer-CLI.git
git remote add veralogix-group  https://github.com/veralogix-group-innovation/Docker-Space-Analyzer-Reclaimer-CLI.git

# Optional convenience: a single "all" remote that pushes to all three at once.
git remote add all https://github.com/VeralogixCatalyst/Docker-Space-Analyzer-Reclaimer-CLI.git
git remote set-url --add --push all https://github.com/VeralogixCatalyst/Docker-Space-Analyzer-Reclaimer-CLI.git
git remote set-url --add --push all https://github.com/AN3S-CREATE/Docker-Space-Analyzer-Reclaimer-CLI.git
git remote set-url --add --push all https://github.com/veralogix-group-innovation/Docker-Space-Analyzer-Reclaimer-CLI.git
```

## Committing & pushing (do this for EVERY change)

```bash
git add -A
git commit -m "…"

# Push individually to each mirror:
git push origin          main
git push an3s-create     main
git push veralogix-group main

# …or, if the "all" remote is configured, push to all three at once:
git push all main
```

Then verify all three heads match:

```bash
for r in origin an3s-create veralogix-group; do
  echo "$r -> $(git ls-remote --heads "$r" main | cut -f1)"
done
git rev-parse HEAD   # local HEAD must equal every remote above
```

## Maintenance, bug-hunting & code review

When investigating errors or performing maintenance, **inspect all three
mirrors** — do not assume they are in sync just because they should be:

1. `git fetch --all` and compare `main` across `origin`, `an3s-create`, and
   `veralogix-group`. If any has drifted, reconcile it first (fast-forward the
   lagging mirrors, or investigate an unexpected divergence before overwriting).
2. Apply fixes once on `main`, then push to **all three** as above.
3. If one mirror has commits the others lack, treat that as a drift incident:
   review those commits and replay them to the others rather than silently
   discarding them.

## Quality gates (run before every push)

```bash
make lint        # ruff + black --check
make typecheck   # mypy strict
make cov         # pytest with the >=80% coverage gate
# or: make all
```

Never push code that fails lint, type-checking, or the test suite to any mirror.

## Working-agreement checklist

- [ ] Change committed on `main`.
- [ ] Quality gates pass (`make all`).
- [ ] Pushed to `origin`, `an3s-create`, and `veralogix-group`.
- [ ] All three remote `main` SHAs match local `HEAD`.

_Last updated: 2026-07-22._

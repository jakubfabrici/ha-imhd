## What does this PR do?

<!-- A short description of the change and why it is needed. Link related issues: "Fixes #123". -->

## Type of change

- [ ] Bug fix
- [ ] New feature / enhancement
- [ ] New city / imhd.sk section
- [ ] Documentation or examples only
- [ ] Refactoring / maintenance

## Checklist

- [ ] `pytest tests -q` passes locally
- [ ] `ruff check .` passes
- [ ] New behaviour is covered by tests (fixtures in `tests/fixtures/` where it makes sense)
- [ ] `strings.json` and both translations (`translations/en.json`, `translations/sk.json`) are updated when user-facing texts changed
- [ ] README / examples are updated when options, entities, attributes or actions changed
- [ ] `CHANGELOG.md` has an entry under "Unreleased"
- [ ] No private data in the diff (tokens, hostnames, home coordinates, personal stops)
- [ ] The change does not increase the load on imhd.sk (no extra polling / connections)

## How was it tested?

<!-- e.g. "Ran it for a day in my HA with stops in ba and ke" -->

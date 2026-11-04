# Contributing

Thanks for taking a look. cv-flow is a small, single-user tool, so contributions are welcome but
the scope is deliberately narrow. This note covers how to get set up and what to expect.

## Scope

cv-flow does two things: it sends a daily ranked job digest, and it tailors your LaTeX resume to a
posting you pick. It does not apply to jobs, and it will not grow a feature that does. It runs as
one process on one person's machine. Changes that keep it simple, reliable, and cheap to run are
the ones most likely to land.

If you want to propose something larger, open an issue first so we can talk it through before you
spend time on it.

## Getting set up

You need Python 3.11 or newer and the Tectonic LaTeX compiler on your PATH.

```bash
git clone https://github.com/satwikbasu/cv-flow.git
cd cv-flow
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Then run the checks:

```bash
pytest            # the test suite
ruff check .      # lint
mypy              # type-check (strict, over src/)
```

All three should be clean before you open a pull request. The resume tests compile a real PDF, so
they need Tectonic installed; the installer in `scripts/install.sh` shows how it is pinned if you
want the same build.

## How changes are made here

The code is built test-first. Write a failing test, watch it fail, add the smallest change that
makes it pass, then commit. Keep commits small and focused, and write them in the
[Conventional Commits](https://www.conventionalcommits.org) style (`feat:`, `fix:`, `chore:`,
`docs:`). Keep the message in plain language.

Match the style of the code around you. Touch only what your change needs; please do not reformat
or refactor unrelated code in the same pull request.

## Reporting a security issue

Do not open a public issue for a security problem. Follow the steps in [SECURITY.md](SECURITY.md)
instead.

## License

cv-flow is licensed under the GNU Affero General Public License v3.0 or later. By contributing, you
agree that your contribution is licensed under the same terms. See [LICENSE](LICENSE).

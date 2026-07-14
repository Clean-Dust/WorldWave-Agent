# Contributing to Worldwave

Thanks for your interest in contributing!

## Getting Started

```bash
git clone https://github.com/Clean-Dust/worldwave.git
cd worldwave
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Development Workflow

1. **Fork** the repository and create your branch from `main`.
2. **Write code** following the existing style and conventions.
3. **Run tests** before submitting:
   ```bash
   pytest tests/ -x -q
   ```
4. **Commit** with clear, descriptive messages.
5. **Push** and open a Pull Request against `main`.

## Pull Request Guidelines

- Keep PRs focused — one feature or fix per PR.
- Include tests for new functionality.
- Update documentation if your change affects user-facing behavior.
- Ensure CI passes (tests, linting).

## Code Style

- Python 3.10+ with type hints.
- Follow PEP 8.
- Use meaningful variable and function names.
- Keep functions small and focused.

## Reporting Issues

Use the issue templates provided:

- **Bug Report** — for something not working as expected.
- **Feature Request** — for proposing new capabilities.

Include as much detail as possible: steps to reproduce, expected vs actual
behavior, environment info, and relevant logs.

## License

By contributing, you agree that your contributions will be licensed under the
MIT License.

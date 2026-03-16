# Contributing

Thanks for your interest in Training Agent! Contributions are welcome.

## Getting Started

```bash
# Fork the repo on GitHub, then:
git clone https://github.com/<your-username>/training-agent.git
cd training-agent

# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Copy config templates
cp .env.example .env
cp attendees.json.example attendees.json
```

## Development Workflow

1. Create a branch from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. Make your changes in `tools/`

3. Run lint:
   ```bash
   ruff check tools/
   ```

4. Run tests:
   ```bash
   pytest tools/tests/ -v
   ```

5. Commit and push:
   ```bash
   git add <files>
   git commit -m "Add your feature description"
   git push origin feature/your-feature-name
   ```

6. Open a Pull Request on GitHub

## Code Style

- Python 3.12+
- Type hints on all functions
- `logging` module instead of `print()`
- All secrets from `.env` via `python-dotenv` — never hardcode
- Georgian text: UTF-8 everywhere
- Retry with exponential backoff for external API calls

## CI Checks

Every PR runs these automatically:

| Check | What it does |
|-------|-------------|
| **Lint** | `ruff check` — no unused imports, no syntax errors |
| **Test** | `pytest` — 523+ tests must pass, 90%+ coverage |
| **Security Audit** | `pip-audit` — no known vulnerabilities |
| **Docker Build** | Verifies the container builds and imports work |

All 4 must pass before merge.

## Project Structure

- `tools/*.py` — Main application code
- `tools/tests/` — Test suite (mirrors `tools/` structure)
- `tools/prompts.py` — AI prompt templates (Georgian)
- `workflows/` — Markdown SOPs
- `CLAUDE.md` — AI assistant instructions (read this first)

## What to Contribute

- Bug fixes
- New features (discuss in an Issue first)
- Test coverage improvements
- Documentation
- Localization (adapting prompts for other languages)
- Performance optimizations

## What to Avoid

- Don't commit `.env`, `credentials.json`, `token.json`, or `attendees.json`
- Don't translate Georgian prompts in `tools/prompts.py` without discussion
- Don't modify production n8n workflow IDs

## Reporting Issues

Open a [GitHub Issue](../../issues) with:
- What you expected
- What happened
- Steps to reproduce
- Relevant logs (redact any API keys)

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).

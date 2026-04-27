# CI/CD Pipeline Documentation

## Pipeline Flow Diagram

```
  push/PR to main
        |
        v
  +===========+
  |    CI      |  (ci.yml)
  +===========+
        |
        +------------------+------------------+
        |                  |                  |
        v                  v                  v
  +-----------+     +-----------+     +-----------+
  |   Lint    |     | Security  |     |  Docker   |
  |           |     |  Audit    |     |  Build    |
  | - syntax  |     | - pip-    |     | - build   |
  | - ruff    |     |   audit   |     |   image   |
  | - mypy    |     |           |     | - verify  |
  | - secret  |     |           |     |   import  |
  |   file    |     |           |     |           |
  |   check   |     |           |     |           |
  +-----------+     +-----------+     +-----------+
        |                                  |
        v                                  |
  +-----------+                            |
  |   Test    | <-- needs: lint            |
  | - pytest  |    (needs: lint) ----------+
  | - 85% cov |
  +-----------+
        |
        | (all CI jobs pass on main)
        v
  +===========+
  |  Deploy   |  (deploy.yml)
  +===========+
        |
        v
  +-----------+
  | Railway   |
  | CLI       |
  | deploy    |
  +-----------+
        |
        v
  +-----------+
  | Health    |
  | Check     |
  | (30x10s)  |
  +-----------+

  push to main (pages/ changed)       push/PR + weekly cron
        |                                    |
        v                                    v
  +===========+                       +===========+
  | Pages     |  (deploy-pages.yml)   | Security  |  (security.yml)
  | Deploy    |                       |   Scan    |
  +===========+                       +===========+
        |                                    |
        v                           +--------+--------+--------+
  +-----------+                     |        |        |        |
  | GitHub    |                     v        v        v        v
  | Pages     |                   Dep.    pip-     Git-    CodeQL
  +-----------+                   Review  audit    leaks
                                 (PRs)  (always) (always) (always)
```

---

## CI Pipeline (`ci.yml`)

**Triggers:** Push to `main`, pull requests targeting `main`.

**Concurrency:** Grouped by branch ref, cancels in-progress runs for the same branch.

**Python version:** 3.12

### Jobs

#### 1. Lint

Runs syntax checking, linting, type checking, and a sensitive file scan.

| Step | What it does |
|------|-------------|
| **Syntax check** | Compiles every `tools/*.py` file (excluding tests and `__init__.py`) with `py_compile` to catch syntax errors early. |
| **Ruff lint** | Runs `ruff check tools/ --select E,W,F --ignore E501,E402` -- checks for pycodestyle errors (E), warnings (W), and pyflakes errors (F). Ignores line-too-long (E501) and module-level import position (E402). |
| **Mypy type check** | Runs `mypy tools/` with `--ignore-missing-imports`, `--warn-return-any`, and `--disallow-untyped-defs`. Currently non-blocking (`|| true`). |
| **Sensitive file block** | Checks git index for forbidden files: `.env`, `credentials.json`, `token.json`, `token_gmail.json`, `attendees.json`. Fails the job if any are found committed. |

#### 2. Test (depends on Lint)

Runs the full test suite with coverage enforcement.

- **Command:** `pytest tools/tests/ -v --tb=short --cov=tools --cov-report=term-missing --cov-fail-under=85`
- **Coverage threshold:** 85% minimum -- the job fails if coverage drops below this.
- **Environment variables:** Test stubs are injected for all required secrets (`WEBHOOK_SECRET`, `ZOOM_*`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `PINECONE_API_KEY`, `GREEN_API_*`, `OPERATOR_PHONE`) so tests can run without real credentials.

#### 3. Security Audit (runs in parallel with other jobs)

- **Command:** `pip-audit --strict --desc on`
- Audits all installed Python dependencies for known vulnerabilities.
- `--strict` mode causes the job to fail on any finding.

#### 4. Docker Build (depends on Lint)

Verifies the Docker image builds and the application can be imported.

| Step | What it does |
|------|-------------|
| **Build image** | `docker build -t training-agent:ci .` |
| **Verify import** | Runs the container with stub environment variables and executes `import tools.app.server` to confirm the application module loads without errors. |

---

## Deploy Pipeline (`deploy.yml`)

**Triggers:**
- Automatically after CI workflow completes successfully on `main`.
- Manual dispatch via `workflow_dispatch`.

**Concurrency:** Grouped as `deploy-railway`, does NOT cancel in-progress deploys (ensures deployments complete).

**Guard condition:** Only runs if CI passed (`workflow_run.conclusion == 'success'`) and the branch is `main`, or if manually triggered.

### Steps

| Step | What it does |
|------|-------------|
| **Install Railway CLI** | `npm install -g @railway/cli` |
| **Deploy** | `railway up --detach` using `RAILWAY_TOKEN` secret. The `--detach` flag returns immediately without waiting for build completion on Railway's side. |
| **Health check** | Polls `$RAILWAY_PUBLIC_URL/health` up to **30 attempts** with **10-second intervals** (5-minute total timeout). Parses the JSON response and looks for `"status": "healthy"`. Fails the workflow if the deployment does not become healthy within the window. Skips gracefully if `RAILWAY_PUBLIC_URL` secret is not configured. |

---

## Pages Deploy (`deploy-pages.yml`)

**Triggers:** Push to `main` when files in the `pages/` directory change (path filter: `pages/**`).

**Concurrency:** Grouped as `pages-deploy`, does NOT cancel in-progress deploys.

**Permissions:** Requires `contents: read`, `pages: write`, and `id-token: write`.

### Jobs

| Job | What it does |
|-----|-------------|
| **Build** | Checks out the repository, verifies the `pages/` directory exists, and uploads it as a GitHub Pages artifact. |
| **Deploy** (depends on Build) | Deploys the artifact to the `github-pages` environment using `actions/deploy-pages@v4`. The deployed URL is output as `steps.deployment.outputs.page_url`. |

---

## Security Scans (`security.yml`)

**Triggers:**
- Push to `main` or `develop`.
- Pull requests targeting `main` or `develop`.
- Weekly scheduled scan: **every Monday at 06:00 UTC**.

**Permissions:** `contents: read`, `security-events: write`.

### Jobs

| Job | When it runs | What it does |
|-----|-------------|-------------|
| **Dependency Review** | Pull requests only | Uses `actions/dependency-review-action@v4` to analyze dependency changes in the PR. Fails on `high` severity or above. |
| **pip-audit** | Always | Installs `pip-audit` and scans `requirements.txt` for known vulnerabilities. Ignores `PYSEC-2024-*` advisories. Currently non-blocking (`|| true`). |
| **Secret Scan (Gitleaks)** | Always | Uses `gitleaks/gitleaks-action@v2` with full git history (`fetch-depth: 0`) to detect leaked secrets, API keys, and credentials in the repository. |
| **CodeQL** | Always | Runs GitHub's CodeQL static analysis for Python. Identifies security vulnerabilities, code quality issues, and common bug patterns. Results appear in the repository's Security tab. |

---

## Summary of Key Thresholds

| Check | Threshold | Blocking? |
|-------|-----------|-----------|
| Test coverage | 85% minimum | Yes |
| Ruff lint (E, W, F rules) | Zero errors | Yes |
| Mypy type check | Non-blocking | No (exits with `|| true`) |
| pip-audit (CI) | Strict mode | Yes |
| pip-audit (security scan) | Non-blocking | No (exits with `|| true`) |
| Dependency review (PRs) | High severity | Yes |
| Sensitive file detection | Any found = fail | Yes |
| Docker build | Must succeed | Yes |
| Deploy health check | 5 min / 30 attempts | Yes |

---

## Required GitHub Secrets

| Secret | Used by | Purpose |
|--------|---------|---------|
| `RAILWAY_TOKEN` | deploy.yml | Authentication for Railway CLI deployment |
| `RAILWAY_PUBLIC_URL` | deploy.yml | Base URL for post-deploy health check (optional) |
| `GITHUB_TOKEN` | security.yml | Automatically provided; used by Gitleaks for repository access |

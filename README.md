# Infra Workflows 🚀

This repository contains reusable GitHub Actions workflows for continuous integration (CI) and infrastructure automation across our projects. By centralizing these workflows, we ensure consistent standards, simplify maintenance, and reduce duplication.

---

## Available Workflows

### 🐍 Python CI (`python-ci.yml`)

A comprehensive, `uv`-powered continuous integration workflow for Python applications. It runs tests with coverage reporting, enforces linting, and checks types.

#### Features

- **Fast Execution**: Uses `uv` ( Astral's extremely fast Python package and environment manager) with automatic caching of dependencies based on `uv.lock`.
- **Linting & Formatting**: Enforces code style using `ruff`.
- **Static Analysis**: Enforces type annotations using `mypy`.
- **Test Coverage**: Runs `pytest` and automatically publishes coverage summaries directly to the GitHub Action Summary and comments on Pull Requests.

#### Prerequisites

For a downstream repository to use this workflow, its codebase must contain:

1. `uv.lock` and a `pyproject.toml` file at the root.
2. `pytest`, `pytest-cov`, `ruff`, and `mypy` declared as dependencies (typically in development/dependency groups synced by `uv sync`).

---

## Integration Guide

To use a reusable workflow, create a workflow file (e.g., `.github/workflows/ci.yml`) in your repository and call this workflow using `jobs.<job_id>.uses`.

### Basic Example

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  ci:
    uses: nateyoder/infra-workflows/.github/workflows/python-ci.yml@v1
    permissions:
      contents: read
      pull-requests: write # Required for test coverage PR comments
    with:
      python-version: "3.12"
```

### Advanced Example with Custom Parameters

You can customize the directories, coverage threshold, or disable specific checks:

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  ci:
    uses: nateyoder/infra-workflows/.github/workflows/python-ci.yml@v1
    permissions:
      contents: read
      pull-requests: write
    with:
      python-version: "3.11"
      src-path: "my_app" # Custom source directory
      tests-path: "tests/unit" # Custom test directory
      cov-fail-under: 80 # Require 80% coverage (default is 90%)
      run-type-check: false # Skip mypy type-checking
```

---

## Workflow Inputs

| Input            | Description                                       | Type      | Default   | Required |
| :--------------- | :------------------------------------------------ | :-------- | :-------- | :------- |
| `python-version` | Version of Python to configure                    | `string`  | `"3.12"`  | No       |
| `src-path`       | Path to Python source directory                   | `string`  | `"src"`   | No       |
| `tests-path`     | Path to Python tests directory                    | `string`  | `"tests"` | No       |
| `cov-fail-under` | Minimum test coverage percentage required to pass | `number`  | `90`      | No       |
| `run-tests`      | Whether to run pytest suite                       | `boolean` | `true`    | No       |
| `run-lint`       | Whether to run ruff linter                        | `boolean` | `true`    | No       |
| `run-type-check` | Whether to run mypy type checker                  | `boolean` | `true`    | No       |

---

## Versioning Strategy: Floating Major Tags 🏷️

We use **Floating Major Tags** to version our reusable workflows. This provides downstream repositories with a balance between stability and ease of updates.

### How it works

- **Floating Tag (`v1`)**: Always points to the latest minor/patch release of the `v1` major version (e.g., `v1.0.0`, `v1.0.1`, `v1.1.0`). Downstream projects referencing `@v1` automatically receive non-breaking bug fixes, performance improvements, and workflow optimizations.
- **Specific Tag (`v1.0.0`)**: Points to a specific, immutable release. Projects referencing `@v1.0.0` will never receive updates unless they manually bump the version string.

### Releasing and Moving Tags (For Maintainers)

When you make changes to a workflow and want to publish a new release:

1. **Commit and push** your changes to the `main` branch.
2. **Tag the specific release** (e.g., `v1.0.0`):

   ```bash
   git tag -a v1.0.0 -m "Release v1.0.0 containing updated pytest coverage configurations"
   git push origin v1.0.0
   ```

3. **Update the floating major tag** (`v1`) to point to the new release:

   ```bash
   # Re-create the v1 tag locally, pointing to the same commit as v1.0.0 (or HEAD)
   git tag -fa v1 -m "Update v1 floating tag to v1.0.0"

   # Force push the updated v1 tag to GitHub
   git push origin v1 --force
   ```

By following this process, any downstream project referencing `uses: nateyoder/infra-workflows/...@v1` will immediately benefit from the new updates on their next workflow run.

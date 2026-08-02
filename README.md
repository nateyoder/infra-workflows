# Infra Workflows 🚀

This repository contains reusable GitHub Actions workflows for continuous integration (CI) and infrastructure automation across our projects. By centralizing these workflows, we ensure consistent standards, simplify maintenance, and reduce duplication.

---

## Available Workflows

### 🐍 Python CI (`python-ci.yml`)

A comprehensive, `uv`-powered continuous integration workflow for Python applications. It runs tests with coverage reporting, enforces linting, and checks types.

#### Features

- **Fast Execution**: Uses `uv` (Astral's extremely fast Python package and environment manager) with automatic caching of dependencies based on `uv.lock`.
- **Linting & Formatting**: Enforces code style using `ruff`.
- **Static Analysis**: Enforces type annotations using `mypy`.
- **Test Coverage**: Runs `pytest` with an optional coverage gate and publishes its summary to the GitHub Action Summary.
- **Private Editable Dependencies**: Optional support for checking out and symlinking private sibling repos before CI runs, enabling `uv` editable path dependencies across repos.
- **Immutable Private Git Dependencies**: Authenticates commit-pinned `git+https://github.com` dependencies without putting credentials in project files, lockfiles, or caches.

#### Prerequisites

For a downstream repository to use this workflow, its codebase must contain:

1. `uv.lock` and a `pyproject.toml` file at the root.
2. `pytest`, `pytest-cov`, `ruff`, and `mypy` declared as dependencies (typically in development/dependency groups synced by `uv sync`). Add `pytest-xdist>=3.2.0` when setting `test-workers` above `0`.

---

### 📊 Metric Cardinality Guard (`metric-cardinality.yml`)

Fails a pull request that adds CloudWatch metric cardinality without acknowledging the cost.

#### Why

CloudWatch bills **$0.30/month per distinct (namespace, metric name, dimension-value)
combination**. A dimension whose cardinality tracks a fleet multiplies the bill by the fleet
size, and nothing in code review makes that visible — an added dimension key looks like any
other dict entry. In July 2026 this cost the recorder fleet **~$2,179/month across 7,264
series, of which 30 were alarmed on** (`nateyoder/pmkt-clients#523`). The worst offender was a
deploy SHA used as a dimension, which re-minted every series on every release.

#### What it does

Scans **added** diff lines for the shapes that create billed series — `put_metric_data`, a
non-empty `Dimensions` list, a `{"Name": ..., "Value": ...}` dimension entry, a new published
metric name, and the AWS CLI equivalents (`put-metric-data`, `put-metric-alarm`, and
`--dimensions Name=...,Value=...` shorthand) — and fails with an explanation of the cost model.

The CLI subcommands fire on their own, without `--dimensions`, exactly as `put_metric_data`
does: a dimensionless publish still mints one billed series, and shelling out should not be a
softer path to a metric than the SDK.

It cannot know whether a metric is worth its cost. It only forces someone to say so in writing.
To acknowledge, put a marker on the detected line or within three committed lines of it:

```python
# metric-budget: 1 fleet series, paged on by pmkt-recorder-data-loss
```

One marker clears the whole publication it sits in — the call and the `Dimensions` nested inside
it — regardless of how many fields the payload carries. The next publication, or a separate edit,
needs its own marker:

```python
# metric-budget: 1 series per recorder (~74), paged on by RecorderDepthStall
cw.put_metric_data(
    Namespace="xp/recorder",
    MetricData=[
        {
            "MetricName": "book_depth",
            "Dimensions": [{"Name": "recorder_id", "Value": rid}],
        }
    ],
)
```

Adjacency is measured in the diff, so publications added as separate edits each need their own
marker even when they end up close together in the file.

If a value is only read while debugging, prefer a log line: Logs Insights queries it for
$0.005/GB scanned and can group by fields far too high-cardinality to be a dimension.

#### Usage

```yaml
jobs:
  metric-cardinality:
    uses: nateyoder/infra-workflows/.github/workflows/metric-cardinality.yml@v1
    permissions:
      contents: read
```

Optional inputs: `paths` (default `*.py *.yml *.yaml *.tf *.json *.sh`) and `base-ref` (defaults
to the PR base branch).

---

## Integration Guide

To use a reusable workflow, create a workflow file (e.g., `.github/workflows/ci.yml`) in your repository and call this workflow using `jobs.<job_id>.uses`.

### Basic Example

```yaml
name: CI

on:
  pull_request:
    branches: [main]

jobs:
  ci:
    uses: nateyoder/infra-workflows/.github/workflows/python-ci.yml@v1
    permissions:
      contents: read
    with:
      python-version: "3.12"
```

### Advanced Example with Custom Parameters

You can customize the directories, coverage threshold, parallelism, or disable specific checks:

```yaml
name: CI

on:
  pull_request:
    branches: [main]

jobs:
  ci:
    uses: nateyoder/infra-workflows/.github/workflows/python-ci.yml@v1
    permissions:
      contents: read
    with:
      python-version: "3.11"
      src-path: "my_app"        # Custom source directory
      tests-path: "tests/unit"  # Custom test directory
      cov-fail-under: 80        # Require 80% coverage (default is 90%)
      coverage: false           # Run tests without coverage, e.g. on drafts
      test-workers: 2           # Opt into pytest-xdist work-stealing
      pytest-args: "--durations=25"
      test-timeout-minutes: 15  # Bound a stalled consolidated quality gate
      run-type-check: false     # Skip mypy type-checking
```

### Private Editable Dependencies

If your project uses `uv` editable path dependencies that reference private sibling repos
(e.g. `perp-arb-ml = { path = "../perp-arb-ml", editable = true }` in `pyproject.toml`),
those paths do not exist on the GitHub Actions runner by default.

Use `editable-path-deps` to specify repos to check out and symlink into place before CI runs.
Each line has the format `<github_repo>:<checkout_path>:<symlink_path>`, where:
- `checkout_path` — where the repo is cloned (relative to workspace)
- `symlink_path` — the path `uv` expects, relative to workspace (e.g. `../perp-arb-ml`)

You must also provide a `repo-read-token` secret — a PAT with read access to the private repos.
Do **not** use `GITHUB_TOKEN`; it is scoped to the current repo and will 404 on sibling repos.

```yaml
name: CI

on:
  pull_request:
    branches: [main]

jobs:
  ci:
    uses: nateyoder/infra-workflows/.github/workflows/python-ci.yml@v1
    permissions:
      contents: read
    secrets:
      repo-read-token: ${{ secrets.CI_REPO_READ_TOKEN }}
    with:
      editable-path-deps: |
        nateyoder/perp-arb-ml:.ci/deps/perp-arb-ml:../perp-arb-ml
        nateyoder/pmkt-clients:.ci/deps/pmkt_clients:../pmkt_clients
```

### Immutable Private Git Dependencies

Projects can retain a full-commit PEP 508 pin to a private GitHub repository:

```toml
[project]
dependencies = [
  "pmkt-clients @ git+https://github.com/nateyoder/pmkt-clients@bd1872bbde4197f80817061dfd43e3655dcfaa2c",
]
```

Forward `repo-read-token` without changing the dependency URL or `uv.lock`:

```yaml
jobs:
  ci:
    uses: nateyoder/infra-workflows/.github/workflows/python-ci.yml@v1
    secrets:
      repo-read-token: ${{ secrets.CI_REPO_READ_TOKEN }}
```

The workflow makes the token available to Git only while `uv lock --check` and
`uv sync --frozen` run, then removes the temporary credential helper. The token
must be a read-only PAT or GitHub App token with access to every referenced
private repository. `GITHUB_TOKEN` cannot read sibling private repositories.

---

## Workflow Inputs

| Input                 | Description                                                    | Type      | Default   | Required |
| :-------------------- | :------------------------------------------------------------- | :-------- | :-------- | :------- |
| `python-version`      | Version of Python to configure                                 | `string`  | `"3.12"`  | No       |
| `src-path`            | Path to Python source directory                                | `string`  | `"src"`   | No       |
| `tests-path`          | Path to Python tests directory                                 | `string`  | `"tests"` | No       |
| `cov-fail-under`      | Minimum test coverage percentage required to pass              | `number`  | `90`      | No       |
| `coverage`            | Whether tests enforce and report coverage                      | `boolean` | `true`    | No       |
| `test-workers`        | pytest-xdist workers; `0` keeps serial pytest                  | `number`  | `0`       | No       |
| `pytest-args`         | Extra space-separated pytest arguments                         | `string`  | `""`     | No       |
| `test-timeout-minutes`| Maximum minutes for the consolidated quality gate               | `number`  | `15`      | No       |
| `run-tests`           | Whether to run pytest suite                                    | `boolean` | `true`    | No       |
| `run-lint`            | Whether to run ruff linter                                     | `boolean` | `true`    | No       |
| `run-type-check`      | Whether to run mypy type checker                               | `boolean` | `true`    | No       |
| `editable-path-deps`  | Newline-separated `repo:checkout_path:symlink_path` entries for private editable deps | `string` | `""` | No |

## Workflow Secrets

| Secret            | Description                                                                                      | Required                              |
| :---------------- | :----------------------------------------------------------------------------------------------- | :------------------------------------ |
| `repo-read-token` | Read-only PAT or GitHub App token for private dependency repos. Do not use `GITHUB_TOKEN` for cross-repo deps. | Yes, for `editable-path-deps` or immutable private Git dependencies |

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

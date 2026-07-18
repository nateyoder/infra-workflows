#!/usr/bin/env bash

set -euo pipefail

auth_dir=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/private-git-auth.XXXXXX")
uv_output="$auth_dir/uv-output.log"
auth_failure=false

cleanup() {
  rm -rf "$auth_dir"
}
trap cleanup EXIT

has_github_git_dependency() {
  grep -Eqs 'git\+https://github\.com/' pyproject.toml 2>/dev/null ||
    grep -Eqs 'git[[:space:]]*=[[:space:]]*"https://github\.com/' uv.lock 2>/dev/null
}

report_auth_failure() {
  if ! has_github_git_dependency; then
    return 1
  fi

  if ! grep -Eqi \
    'Authentication failed|could not read Username|Invalid username or token|Repository not found|terminal prompts disabled|returned error: (401|403)' \
    "$uv_output"; then
    return 1
  fi

  if [ -z "${REPO_READ_TOKEN:-}" ]; then
    echo "::error title=Private Git dependency authentication required::A git+https://github.com dependency could not be read. Pass repo-read-token with read access to every private dependency repository; GITHUB_TOKEN cannot read sibling private repositories."
  else
    echo "::error title=Private Git dependency authentication failed::repo-read-token is invalid or lacks read access to a git+https://github.com dependency. Verify the token and its repository permissions."
  fi
}

run_uv() {
  local -a pipeline_status
  local uv_status tee_status

  set +e
  "$@" 2>&1 | tee "$uv_output"
  pipeline_status=("${PIPESTATUS[@]}")
  set -e

  uv_status=${pipeline_status[0]}
  tee_status=${pipeline_status[1]}

  if [ "$uv_status" -ne 0 ]; then
    if report_auth_failure; then
      auth_failure=true
    fi
    return "$uv_status"
  fi

  return "$tee_status"
}

export GIT_TERMINAL_PROMPT=0

if [ -n "${REPO_READ_TOKEN:-}" ]; then
  askpass="$auth_dir/askpass.sh"
  cat >"$askpass" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in
  "Username for 'https://github.com'"*) printf '%s\n' 'x-access-token' ;;
  "Password for 'https://github.com'"* | "Password for 'https://x-access-token@github.com'"*)
    printf '%s\n' "$REPO_READ_TOKEN"
    ;;
  *) exit 1 ;;
esac
EOF
  chmod 700 "$askpass"
  export GIT_ASKPASS="$askpass"
fi

if ! run_uv uv lock --check; then
  if [ "$auth_failure" != true ]; then
    echo "::error title=uv.lock is out of date::The committed uv.lock does not match what uv resolves here. Run 'uv lock' and commit the result."
    if [ -n "${EDITABLE_PATH_DEPS:-}" ]; then
      echo "::notice title=editable path deps::This project has editable path dependencies. The lock may have been generated locally against a path-dep checkout that differs from the ref CI clones (its origin/main). Regenerate the lock against origin/main and commit it, or keep your local sibling checkouts in sync with origin/main."
    fi
  fi
  exit 1
fi

run_uv uv sync --frozen

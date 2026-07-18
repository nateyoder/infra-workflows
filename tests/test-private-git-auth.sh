#!/usr/bin/env bash

set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
install_script="$repo_root/.github/actions/setup-python-env/install-dependencies.sh"
fixture_root=$(mktemp -d "${TMPDIR:-/tmp}/private-git-auth-test.XXXXXX")
trap 'rm -rf "$fixture_root"' EXIT

commit=bd1872bbde4197f80817061dfd43e3655dcfaa2c
dependency_url="git+https://github.com/nateyoder/pmkt-clients@$commit"
token=fixture-private-read-token

new_consumer() {
  local name=$1
  local consumer="$fixture_root/$name"

  mkdir -p "$consumer/bin" "$consumer/runner-temp"
  cat >"$consumer/pyproject.toml" <<EOF
[project]
name = "private-git-consumer"
version = "0.1.0"
dependencies = ["pmkt-clients @ $dependency_url"]
EOF
  cat >"$consumer/uv.lock" <<EOF
version = 1
source = { git = "https://github.com/nateyoder/pmkt-clients?rev=$commit#$commit" }
EOF
  printf '%s\n' "$consumer"
}

consumer=$(new_consumer success)
cat >"$consumer/bin/uv" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

[ "$GIT_TERMINAL_PROMPT" = 0 ]
[ -x "$GIT_ASKPASS" ]
[ "$("$GIT_ASKPASS" 'Username for https://github.com')" = x-access-token ]
[ "$("$GIT_ASKPASS" 'Password for https://github.com')" = "$EXPECTED_TOKEN" ]
if "$GIT_ASKPASS" 'Password for https://gitlab.com'; then
  echo 'credential helper answered for a non-GitHub host' >&2
  exit 1
fi
printf '%s\n' "$GIT_ASKPASS" >"$ASKPASS_RECORD"
printf '%s\n' "$*" >>"$UV_CALLS"
EOF
chmod +x "$consumer/bin/uv"

success_output="$consumer/success-output.log"
(
  cd "$consumer"
  PATH="$consumer/bin:$PATH" \
    REPO_READ_TOKEN="$token" \
    EDITABLE_PATH_DEPS='' \
    EXPECTED_TOKEN="$token" \
    ASKPASS_RECORD="$consumer/askpass-record" \
    UV_CALLS="$consumer/uv-calls" \
    RUNNER_TEMP="$consumer/runner-temp" \
    "$install_script"
) >"$success_output" 2>&1

printf 'lock --check\nsync --frozen\n' >"$consumer/expected-uv-calls"
diff -u "$consumer/expected-uv-calls" "$consumer/uv-calls"
askpass_path=$(cat "$consumer/askpass-record")
[ ! -e "$askpass_path" ] || {
  echo "temporary credential helper was not removed" >&2
  exit 1
}
grep -F "$dependency_url" "$consumer/pyproject.toml" >/dev/null
grep -F "$commit#$commit" "$consumer/uv.lock" >/dev/null
if grep -R -F "$token" "$consumer" >/dev/null; then
  echo "credential leaked into fixture output or files" >&2
  exit 1
fi

consumer=$(new_consumer missing-token)
cat >"$consumer/bin/uv" <<'EOF'
#!/usr/bin/env bash
echo "fatal: could not read Username for 'https://github.com': terminal prompts disabled" >&2
exit 1
EOF
chmod +x "$consumer/bin/uv"

missing_output="$consumer/missing-output.log"
if (
  cd "$consumer"
  PATH="$consumer/bin:$PATH" \
    REPO_READ_TOKEN='' \
    EDITABLE_PATH_DEPS='' \
    RUNNER_TEMP="$consumer/runner-temp" \
    "$install_script"
) >"$missing_output" 2>&1; then
  echo "missing token unexpectedly succeeded" >&2
  exit 1
fi
grep -F 'Private Git dependency authentication required' "$missing_output" >/dev/null
grep -F 'Pass repo-read-token with read access' "$missing_output" >/dev/null
if grep -F 'uv.lock is out of date' "$missing_output" >/dev/null; then
  echo "authentication failure was misreported as a stale lockfile" >&2
  exit 1
fi

consumer=$(new_consumer invalid-token)
cat >"$consumer/bin/uv" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$GIT_ASKPASS" >"$ASKPASS_RECORD"
echo 'remote: Invalid username or token.' >&2
exit 1
EOF
chmod +x "$consumer/bin/uv"

invalid_output="$consumer/invalid-output.log"
if (
  cd "$consumer"
  PATH="$consumer/bin:$PATH" \
    REPO_READ_TOKEN="$token" \
    EDITABLE_PATH_DEPS='' \
    ASKPASS_RECORD="$consumer/askpass-record" \
    RUNNER_TEMP="$consumer/runner-temp" \
    "$install_script"
) >"$invalid_output" 2>&1; then
  echo "invalid token unexpectedly succeeded" >&2
  exit 1
fi
grep -F 'Private Git dependency authentication failed' "$invalid_output" >/dev/null
grep -F 'invalid or lacks read access' "$invalid_output" >/dev/null
askpass_path=$(cat "$consumer/askpass-record")
[ ! -e "$askpass_path" ] || {
  echo "temporary credential helper survived a failed install" >&2
  exit 1
}
if grep -R -F "$token" "$consumer" >/dev/null; then
  echo "invalid credential leaked into fixture output or files" >&2
  exit 1
fi

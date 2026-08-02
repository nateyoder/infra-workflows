#!/usr/bin/env bash

# Builds a clone of this repository as it will exist *after* a squash merge, and echoes its path.
#
# Three separate defects have shipped green and broken only once the branch was gone (PR #8
# R5-F1, R6-F1, and the stale pin in R4-F1). They share one cause: a working tree carries its
# branch's history, so anything that resolves a commit -- a self-pin, a fixture, a fetch -- finds
# objects on the branch that a squash-merged `main` will never contain. Every suite passes, and
# the failure appears on the next PR instead.
#
# The clone this produces reproduces that state exactly:
#   * `main` is a squash commit of the current tree onto the current base, so branch commits are
#     unreachable from it, as after a real squash merge with the branch deleted;
#   * pinned commits are published under `refs/pull/*` on the synthetic origin only, matching
#     GitHub, which serves them on request but never fetches them into a clone by default;
#   * the clone is `--single-branch --branch main`, which is what `actions/checkout` produces.
#
# Sourced by tests; sets `post_merge_clone` to the resulting checkout.

build_post_merge_clone() {
  local repo_root=$1
  local scratch=$2
  local origin="$scratch/origin.git"

  git init -q --bare "$origin"

  local base_head feature_tree
  # `origin/main` is what CI has; fall back to the merge base for a local run without it.
  base_head=$(git -C "$repo_root" rev-parse --verify --quiet origin/main) \
    || base_head=$(git -C "$repo_root" merge-base HEAD main 2>/dev/null) \
    || base_head=$(git -C "$repo_root" rev-parse HEAD)
  # Snapshot the working tree, not HEAD, via a throwaway index: in CI the two are identical, and
  # locally it means the harness reflects the change being made rather than the last commit.
  feature_tree=$(
    GIT_INDEX_FILE="$scratch/snapshot.index" git -C "$repo_root" read-tree HEAD \
      && GIT_INDEX_FILE="$scratch/snapshot.index" git -C "$repo_root" add -A \
      && GIT_INDEX_FILE="$scratch/snapshot.index" git -C "$repo_root" write-tree
  )

  git -C "$repo_root" push -q "file://$origin" "$base_head:refs/heads/base"
  # Sends the objects the squash commit is built from; refs/pull/* so it stays off every branch.
  git -C "$repo_root" push -q "file://$origin" "HEAD:refs/pull/sim/head"
  # write-tree can produce objects no ref reaches, so hand them over directly.
  GIT_INDEX_FILE="$scratch/snapshot.index" git -C "$repo_root" \
    pack-objects --quiet --stdout --revs <<<"$feature_tree" \
    | git --git-dir="$origin" unpack-objects -q 2>/dev/null || true

  # Publish every self-pin under refs/pull/*, reachable from no branch -- exactly how a merged
  # PR's commits remain fetchable on GitHub. Fetch any the local checkout is missing first,
  # otherwise the push has nothing to send.
  local pin index=0
  while IFS= read -r pin; do
    [ -n "$pin" ] || continue
    index=$((index + 1))
    git -C "$repo_root" cat-file -e "${pin}^{commit}" 2>/dev/null \
      || git -C "$repo_root" fetch --quiet --no-tags origin "$pin" 2>/dev/null \
      || continue
    git -C "$repo_root" push -q "file://$origin" "$pin:refs/pull/sim/pin-$index"
  done < <(
    grep -rhoE 'nateyoder/infra-workflows/[^ @]+@[0-9a-f]{40}' "$repo_root/.github" \
      | sed 's/.*@//' | sort -u
  )

  local squash_head
  squash_head=$(
    git -c user.name=Fixture -c user.email=fixture@example.invalid \
      --git-dir="$origin" commit-tree "$feature_tree" -p "$base_head" \
      -m 'synthetic squash merge'
  )
  git --git-dir="$origin" update-ref refs/heads/main "$squash_head"
  # Drop the branch the pins came in on, so only the squash commit is reachable.
  git --git-dir="$origin" update-ref -d refs/heads/base

  git clone -q --single-branch --branch main "file://$origin" "$scratch/checkout"
  post_merge_clone="$scratch/checkout"
}

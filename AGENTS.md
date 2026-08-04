# AGENTS.md

## Code Quality

Prefer clear, simple, and concise code. Every line is a potential bug, but never sacrifice
readability for brevity.

## CI Guards

- Guards fail closed: prefer an actionable false positive to silently missing a violation.
- Editing `.github/actions/` invalidates its immutable self-pin. Commit the action change first,
  then repin the calling workflow to that commit in a separate follow-up commit.
- The mutation harness and the clone-based fixtures read the **committed** tree, never the working
  tree. Commit a new guard, suite, or fixture before running the checks over it, or they test the
  state before your change and report success. A fixture that *invents* a file rather than editing
  a tracked one must `git add` it, because discovery walks `git ls-files`.

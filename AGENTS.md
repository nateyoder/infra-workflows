# AGENTS.md

## Code Quality

Prefer clear, simple, and concise code. Every line is a potential bug, but never sacrifice
readability for brevity.

## CI Guards

- Guards fail closed: prefer an actionable false positive to silently missing a violation.
- Editing `.github/actions/` invalidates its immutable self-pin. Commit the action change first,
  then repin the calling workflow to that commit in a separate follow-up commit.

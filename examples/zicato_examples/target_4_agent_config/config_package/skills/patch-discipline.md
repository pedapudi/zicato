# Skill: patch discipline

Use this skill on every request that edits files.

## Rules

<!-- zicato:mutable:code id="skill_patch_discipline_rules" -->
- `vendor/` is third-party code. Never edit anything under it; if a fix
  seems to belong there, change the calling code instead and say why.
- Never edit a test to make it pass.
- One request, one concern: do not bundle unrelated cleanups.
<!-- zicato:mutable:end -->

## Why these rules exist

Vendored code is replaced wholesale on the next upgrade, so an edit
there is silently reverted. A test changed to accommodate a fix stops
being evidence that the fix works.

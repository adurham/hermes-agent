# Group g18-desktop-a — FORK.md guidance

Files: apps/desktop/package.json, apps/desktop/src/app/chat/session-drag.ts,
apps/desktop/src/app/chat/sidebar/index.tsx,
apps/desktop/src/app/chat/sidebar/order.test.ts,
apps/desktop/src/app/chat/sidebar/projects/entered-content.tsx,
apps/desktop/src/app/chat/sidebar/projects/workspace-group.tsx

## General desktop merge policy
Upstream splits desktop "god-files" constantly, and the split does NOT always show up as
a clean modify/delete rename — sometimes an ordinary-looking `UU` conflict is actually
part of a larger extraction (e.g. a previous sync saw `hermes.ts` split into `src/api/*`
while presenting as a normal two-sided conflict). If a file in your list has an unusually
small diff on one side relative to its conflict markers, check whether upstream extracted
logic OUT of it into a new sibling file — `git show v2026.9.14 --stat` around the
relevant commit range, or just grep for a plausible new import target, before resolving
the conflict as if it were a simple two-sided edit.

## apps/desktop/src/app/chat/session-drag.ts
A prior sync's merge left a dead `DoubleTapContext` parameter (`double: DoubleTapContext
| undefined`) across several files after upstream retired that type entirely (replaced
by a `stripVisible` / `strip-visibility.ts` resolver). If you see `DoubleTapContext`
anywhere in this file's signatures or call sites and it's not actually imported/defined
anywhere in the current tree, DROP it from the signature and any call site — this
produces a TS2304 (cannot find name) error if left in. `grep -rn DoubleTapContext
apps/desktop/src/` (read-only, safe) should return zero hits when you're done with this
specific file's portion.

## apps/desktop/package.json
This is JSON — conflicts here are almost always dependency-version bumps or new
script/dependency entries. Merge additively: take BOTH sides' added dependencies/scripts
unless they're the exact same key with different version pins, in which case take the
higher/newer pin (upstream is generally the newer baseline in a sync). Do NOT let this
resolution break JSON syntax — validate with a JSON parser after resolving, not just a
marker grep (JSON has no native compile-check, so be extra careful with trailing commas).

## No specific prior guidance found for
apps/desktop/src/app/chat/sidebar/index.tsx, apps/desktop/src/app/chat/sidebar/
order.test.ts, apps/desktop/src/app/chat/sidebar/projects/entered-content.tsx,
apps/desktop/src/app/chat/sidebar/projects/workspace-group.tsx — use the common-guidance
default. Flag design-collision picks explicitly; these will need new FORK.md entries.

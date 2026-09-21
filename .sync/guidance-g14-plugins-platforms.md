# Group g14-plugins-platforms — FORK.md guidance

Files: plugins/platforms/a2a/README.md, plugins/platforms/a2a/__init__.py,
plugins/platforms/a2a/adapter.py, plugins/platforms/a2a/security.py,
plugins/platforms/a2a/tools.py, plugins/platforms/discord/adapter.py,
plugins/platforms/sms/adapter.py, plugins/spotify/__init__.py, plugins/web/ddgs/provider.py

## General platform-adapter policy
Per this repo's plugins/AGENTS.md: platform adapters have token-lock and scoped-secret
rules (canonical examples: irc, feishu adapters). If any of these adapter files show a
conflict around credential/token scoping, prefer the MORE restrictive/scoped side unless
you can positively confirm upstream's version is equivalent — token/secret handling bugs
are a security regression class, not a style choice.

## plugins/web/ddgs/provider.py
This is a keyless-ring member (DDGS = DuckDuckGo search). Related to the Tavily
keyed-only decision (see group g04's `agent/web_search_registry.py` note) — DDGS is a
different, legitimately-keyless provider and is NOT part of the tavily controversy.
Standard `plugins/web/_common.py` `BaseWebSearchProvider` pattern applies if upstream
refactored the shared web-provider scaffolding (see the sibling `exa`/`parallel`/
`firecrawl`/`keenable`/`tavily` providers for the established shape — NAME/DISPLAY_NAME/
KEY_ENV/EXTRACT/KEYLESS class attributes, `is_available`/`is_keyless_available` via the
ABC defaults). If this file hasn't been migrated to that shared pattern yet and upstream's
diff does the migration, take upstream's migrated version and re-home any fork-specific
DDGS behavior into the new shape.

## No specific prior guidance found for
plugins/platforms/a2a/* (all 5 files), plugins/platforms/discord/adapter.py,
plugins/platforms/sms/adapter.py, plugins/spotify/__init__.py — use the common-guidance
default (adopt upstream's structure, re-home fork additions on top of it). Flag
design-collision picks explicitly; these will need new FORK.md entries.

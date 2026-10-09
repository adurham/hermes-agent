"""Portable-glyph registry + cluster normalization for prompt_toolkit chrome.

prompt_toolkit's renderer diffs its Screen model cell-by-cell and never rewrites
cells whose model content is unchanged. Any glyph whose painted width differs
from pt's model therefore strands stale cells to its RIGHT across repaints —
the "impossible timer" corruption (status bar's "🗜️" badge: pt model 1 cell,
kitty paints 2, seeded "⏲ 0s" stranded its '0' so single-digit seconds showed
as "80s"/"70s").

Defense layers (see FORK.md 2026-10-06):
  1. This module: a committed PORTABLE set + grapheme-cluster normalization of
     dynamic text (the runtime half), and the allowlist the chrome lint
     (tests/hermes_cli/test_pt_chrome_no_vs16.py) enforces on static strings.
  2. The width-engine matrix test: every codepoint in use scored across
     pt/wcwidth, xterm.js Unicode-11, and the UTR#51 modern-terminal model.
  3. The diff-replay harness: simulates pt's diff arithmetic against a painted
     grid from each engine and asserts no stale cells survive.

Rule of the house: a NEW chrome glyph = a deliberate edit here (with class
rationale) + the matrix passing. Sequences (ZWJ, VS-15/16, keycaps, regional
indicators, skin tones, tag chars, bidi controls) are structurally banned from
chrome; dynamic text is normalized to their base codepoint by
``normalize_for_chrome``.
"""
from __future__ import annotations

# ── banned sequence/format machinery ────────────────────────────────────────
# Any of these appearing in pt-rendered text can change painted width relative
# to pt's per-codepoint model, or reorder the grid (bidi). Static chrome may
# contain none of them; dynamic text is normalized through them.
BANNED_CODEPOINTS = frozenset({
    0x200B,  # ZERO WIDTH SPACE
    0x200C,  # ZERO WIDTH NON-JOINER
    0x200D,  # ZERO WIDTH JOINER (👨‍💻-style clusters paint ≠ sum of parts)
    0x200E,  # LEFT-TO-RIGHT MARK
    0x200F,  # RIGHT-TO-LEFT MARK
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,  # bidi embedding/override controls
    0x2066, 0x2067, 0x2068, 0x2069,          # bidi isolates
    0x20E3,  # COMBINING ENCLOSING KEYCAP (1️⃣)
    0xFE0E,  # VARIATION SELECTOR-15 (force text presentation)
    0xFE0F,  # VARIATION SELECTOR-16 (force emoji presentation)
}) | set(range(0xE0020, 0xE0080))                     # tag chars (flag sequences)
BANNED_CODEPOINTS = frozenset(
    BANNED_CODEPOINTS
    | {0x1F1E6 + i for i in range(26)}                # regional indicators (pairs paint 2)
    | {0x1F3FB + i for i in range(5)}                 # Fitzpatrick skin-tone modifiers
)


def normalize_for_chrome(text: str) -> str:
    """Dynamic text -> text safe for a prompt_toolkit-rendered line.

    Drops every banned sequence/format codepoint. For a cluster whose width is
    sequence-dependent, dropping the sequence machinery is the correct policy
    (each surviving codepoint is then a single-codepoint width, which every
    engine agrees on):
    "🗜️" -> "🗜", "👨‍💻" -> "👨💻" (ZWJ removed; the two emoji are then two
    independent 2-cell graphemes), "1️⃣" -> "1", "👍🏽" -> "👍",
    "🇨🇳" -> "" (both regional indicators drop), VS-15 drops to the base.
    """
    return "".join(ch for ch in text if ord(ch) not in BANNED_CODEPOINTS)


# ── the portable set (layer-1 allowlist) ────────────────────────────────────
# Static chrome strings may contain: ASCII, plus exactly the codepoints below.
# This list IS the set of non-ASCII codepoints the pt-rendered chrome files use
# (verified by the allowlist lint); adding a glyph to chrome = a deliberate edit
# here + tests/hermes_cli/test_width_engine_matrix.py passing.
#
# Classes:
#   narrow-solid : EAW N/Na/H (no emoji) — 1 cell in every engine we measure.
#   wide-solid   : EAW W/F or Emoji_Presentation=Yes — 2 cells everywhere.
#   accepted-risk: EAW=Ambiguous — 1 cell on kitty/iTerm2/Terminal.app by
#                  default (pixel ground truth), 2 under iTerm2's
#                  "ambiguous-width as double-width" pref or CJK locales.
#                  Accepted deliberately: the matrix surfaces every one in its
#                  ambiguous-wide column, so a regression is a visible decision.
NARROW_SOLID = "narrow-solid"
WIDE_SOLID = "wide-solid"
ACCEPTED_RISK = "accepted-risk"

PORTABLE_EXTRA_CODEPOINTS: dict[int, str] = {
    0x000b7: ACCEPTED_RISK,  # ·
    0x000bb: NARROW_SOLID,  # »
    0x003a3: ACCEPTED_RISK,  # Σ
    0x02014: ACCEPTED_RISK,  # —
    0x02026: ACCEPTED_RISK,  # …
    0x0203a: NARROW_SOLID,  # ›
    0x02190: ACCEPTED_RISK,  # ←
    0x02191: ACCEPTED_RISK,  # ↑
    0x02192: ACCEPTED_RISK,  # →
    0x02193: ACCEPTED_RISK,  # ↓
    0x02299: ACCEPTED_RISK,  # ⊙
    0x02387: NARROW_SOLID,  # ⎇
    0x023ce: NARROW_SOLID,  # ⏎
    0x023e9: WIDE_SOLID,  # ⏩
    0x023f1: NARROW_SOLID,  # ⏱
    0x023f2: NARROW_SOLID,  # ⏲
    0x023f3: WIDE_SOLID,  # ⏳
    0x023f8: NARROW_SOLID,  # ⏸
    0x02500: ACCEPTED_RISK,  # ─
    0x02502: ACCEPTED_RISK,  # │
    0x02514: ACCEPTED_RISK,  # └
    0x0256d: ACCEPTED_RISK,  # ╭
    0x0256e: ACCEPTED_RISK,  # ╮
    0x0256f: ACCEPTED_RISK,  # ╯
    0x02570: ACCEPTED_RISK,  # ╰
    0x02580: ACCEPTED_RISK,  # ▀
    0x02581: ACCEPTED_RISK,  # ▁
    0x02582: ACCEPTED_RISK,  # ▂
    0x02583: ACCEPTED_RISK,  # ▃
    0x02584: ACCEPTED_RISK,  # ▄
    0x02585: ACCEPTED_RISK,  # ▅
    0x02586: ACCEPTED_RISK,  # ▆
    0x02587: ACCEPTED_RISK,  # ▇
    0x02588: ACCEPTED_RISK,  # █
    0x0258f: ACCEPTED_RISK,  # ▏
    0x02591: NARROW_SOLID,  # ░
    0x025b6: ACCEPTED_RISK,  # ▶
    0x025b8: NARROW_SOLID,  # ▸
    0x025ba: NARROW_SOLID,  # ►
    0x025c6: ACCEPTED_RISK,  # ◆
    0x025c9: NARROW_SOLID,  # ◉
    0x025ce: ACCEPTED_RISK,  # ◎
    0x025cf: ACCEPTED_RISK,  # ●
    0x025f7: NARROW_SOLID,  # ◷
    0x02624: NARROW_SOLID,  # ☤
    0x02695: NARROW_SOLID,  # ⚕
    0x02699: NARROW_SOLID,  # ⚙
    0x026a0: NARROW_SOLID,  # ⚠
    0x026a1: WIDE_SOLID,  # ⚡
    0x026d3: ACCEPTED_RISK,  # ⛓
    0x026d4: WIDE_SOLID,  # ⛔
    0x02705: WIDE_SOLID,  # ✅
    0x0270e: NARROW_SOLID,  # ✎
    0x02713: NARROW_SOLID,  # ✓
    0x02714: NARROW_SOLID,  # ✔
    0x02718: NARROW_SOLID,  # ✘
    0x0274c: WIDE_SOLID,  # ❌
    0x02754: WIDE_SOLID,  # ❔ (todo board: unknown-status marker)
    0x0276f: NARROW_SOLID,  # ❯
    0x1f3a4: WIDE_SOLID,  # 🎤
    0x1f465: WIDE_SOLID,  # 👥
    0x1f4be: WIDE_SOLID,  # 💾
    0x1f4cb: WIDE_SOLID,  # 📋 (todo board header)
    0x1f4cc: WIDE_SOLID,  # 📌
    0x1f4dd: WIDE_SOLID,  # 📝
    0x1f500: WIDE_SOLID,  # 🔀
    0x1f504: WIDE_SOLID,  # 🔄 (todo board in_progress, via tools.todo_tool._STATUS_MARKERS)
    0x1f510: WIDE_SOLID,  # 🔐
    0x1f511: WIDE_SOLID,  # 🔑
    0x1f512: WIDE_SOLID,  # 🔒
    0x1f5dc: NARROW_SOLID,  # 🗜
    0x1f9e0: WIDE_SOLID,  # 🧠
}
ACCEPTED_RISK_SET = frozenset(cp for cp, cls in PORTABLE_EXTRA_CODEPOINTS.items()
                              if cls == ACCEPTED_RISK)


def is_portable_codepoint(ch: str) -> bool:
    """True when a single codepoint is allowed in static pt chrome."""
    cp = ord(ch)
    if cp < 0x80:
        return True
    if cp in BANNED_CODEPOINTS:
        return False
    return cp in PORTABLE_EXTRA_CODEPOINTS

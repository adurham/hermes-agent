"""Render Slack ``rich_text`` element trees to readable lines (quotes, lists, code).

Sibling of ``adapter._extract_text_from_slack_blocks``. The inline renderer is passed in rather
than imported because the adapter loads both as a package and as a flat plugin dir.
"""

from typing import Callable

InlineRenderer = Callable[[list], str]


def _append_line(parts: list[str], text: str, quote_depth: int = 0, bullet: str = "") -> None:
    if not text or not text.strip():
        return
    prefix = ((">" * quote_depth) + " ") if quote_depth else ""
    parts.append(f"{prefix}{bullet}{text}".rstrip())


def _walk_section(elem: dict, parts: list[str], render: InlineRenderer,
                  quote_depth: int, bullet: str) -> None:
    _append_line(parts, render(elem.get("elements", [])), quote_depth, bullet)


def _walk_quote(elem: dict, parts: list[str], render: InlineRenderer,
                quote_depth: int, bullet: str) -> None:
    walk_rich_text_elements(elem.get("elements", []), parts, render, quote_depth=quote_depth + 1)


def _walk_list(elem: dict, parts: list[str], render: InlineRenderer,
               quote_depth: int, bullet: str) -> None:
    list_style = elem.get("style")
    for idx, item in enumerate(elem.get("elements", [])):
        item_bullet = "• " if list_style == "bullet" else f"{idx + 1}. "
        walk_rich_text_elements([item], parts, render, quote_depth=quote_depth, bullet=item_bullet)


def _walk_preformatted(elem: dict, parts: list[str], render: InlineRenderer,
                       quote_depth: int, bullet: str) -> None:
    code_lines = [
        render(
            child.get("elements", [])
            if child.get("type", "") == "rich_text_section"
            else [child])
        for child in elem.get("elements", [])]
    code_text = "\n".join(line for line in code_lines if line)
    if code_text:
        lang = elem.get("language", "")
        _append_line(parts, f"```{lang}\n{code_text}\n```", quote_depth, bullet)


def _walk_inline(elem: dict, parts: list[str], render: InlineRenderer,
                 quote_depth: int, bullet: str) -> None:
    _append_line(parts, render([elem]), quote_depth, bullet)


_RICH_TEXT_WALKERS = {
    "rich_text_section": _walk_section,
    "rich_text_quote": _walk_quote,
    "rich_text_list": _walk_list,
    "rich_text_preformatted": _walk_preformatted,
}


def walk_rich_text_elements(elements: list, parts: list[str], render: InlineRenderer,
                            quote_depth: int = 0, bullet: str = "") -> None:
    """Append one rendered line per element to ``parts``; unknown types render inline."""
    for elem in elements:
        walker = _RICH_TEXT_WALKERS.get(elem.get("type", ""), _walk_inline)
        walker(elem, parts, render, quote_depth, bullet)

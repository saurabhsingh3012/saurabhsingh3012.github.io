#!/usr/bin/env python3
"""Static sanity checks for self-contained HTML pages.

Catches the class of bug that shipped a visibly broken upload box: a <label>
styled as a drop zone with 2.5rem of padding but no `display`, which stays
inline, so the padding never expands its line box and the dashed border
overflows into whatever follows.

Runs without a browser, so it works in CI. Checks:

  1. tag balance
  2. box properties applied to inline-by-default elements with no display set
  3. classes used in markup with no matching CSS rule (typos)
  4. internal links and asset references that do not resolve on disk
  5. document.getElementById targets missing from the markup
  6. a <label for=...> that also nests its input (fires the picker twice)

Usage:  python tools/checkpage.py [path ...]        # defaults to **/*.html
Exit 1 if any ERROR is found. WARNs do not fail the build.
"""

from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr"}

# Elements whose default display is inline. Giving these vertical padding,
# height, or a width without an explicit `display` is almost always a bug.
INLINE_BY_DEFAULT = {"a", "span", "label", "strong", "em", "b", "i", "small",
                     "code", "cite", "q", "abbr", "sub", "sup", "u", "s", "mark"}

BOX_PROPS = re.compile(
    r"\b(padding(?:-(?:top|bottom|block))?|height|width|"
    r"margin-(?:top|bottom|block))\s*:\s*([^;}]+)", re.I)
DISPLAY = re.compile(r"\bdisplay\s*:\s*([a-z-]+)", re.I)
LENGTH = re.compile(r"(-?[\d.]+)\s*(px|rem|em|%|vh|vw)?")

# A tiny padding-bottom paired with a border-bottom is the standard
# underline-offset idiom on inline links and is intentional. Only flag vertical
# box sizing large enough to actually be laying something out.
SIGNIFICANT_PX = 8.0
_TO_PX = {"px": 1.0, "rem": 16.0, "em": 16.0, "%": 1.0, "vh": 8.0, "vw": 8.0}


def _is_significant(prop: str, value: str) -> bool:
    """Would this declaration meaningfully size an inline box?"""
    if prop.lower() in {"height", "width"}:
        return "auto" not in value.lower()
    parts = value.strip().split()
    # padding shorthand: 1 value = all sides, 2+ = vertical is first
    vertical = parts[0] if parts else ""
    if prop.lower() in {"padding-bottom", "padding-top", "padding-block",
                        "margin-top", "margin-bottom", "margin-block"}:
        vertical = parts[0] if parts else ""
    m = LENGTH.match(vertical)
    if not m:
        return False
    try:
        magnitude = abs(float(m.group(1))) * _TO_PX.get(m.group(2) or "px", 1.0)
    except ValueError:
        return False
    return magnitude >= SIGNIFICANT_PX


class Doc(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, int]] = []
        self.unclosed: list[tuple[str, int]] = []
        self.mismatched: list[tuple[str, int]] = []
        self.classes: set[str] = set()
        self.ids: set[str] = set()
        self.tag_classes: dict[str, set[str]] = {}
        self.refs: list[tuple[str, str, int]] = []
        self.labels: list[tuple[int, str | None, bool]] = []
        self._label_depth: int | None = None
        self._label: tuple[int, str | None, bool] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        line = self.getpos()[0]

        for cls in (a.get("class") or "").split():
            self.classes.add(cls)
            self.tag_classes.setdefault(cls, set()).add(tag)
        if a.get("id"):
            self.ids.add(a["id"])
        for attr in ("href", "src"):
            if a.get(attr):
                self.refs.append((attr, a[attr], line))

        if tag == "label":
            self._label = (line, a.get("for"), False)
            self._label_depth = len(self.stack)
        elif tag == "input" and self._label is not None:
            self._label = (self._label[0], self._label[1], True)

        if tag not in VOID:
            self.stack.append((tag, line))

    def handle_endtag(self, tag: str) -> None:
        if tag == "label" and self._label is not None:
            self.labels.append(self._label)
            self._label = None
            self._label_depth = None
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()
        else:
            for i in range(len(self.stack) - 1, -1, -1):
                if self.stack[i][0] == tag:
                    self.mismatched.append(self.stack[i])
                    del self.stack[i]
                    return
            self.mismatched.append((tag, self.getpos()[0]))

    def close(self) -> None:  # type: ignore[override]
        super().close()
        self.unclosed = list(self.stack)


def css_blocks(html: str) -> list[tuple[str, str, int]]:
    """(selector, body, line) for every rule inside <style>."""
    out: list[tuple[str, str, int]] = []
    for m in re.finditer(r"<style[^>]*>(.*?)</style>", html, re.S | re.I):
        base = html[: m.start()].count("\n") + 1
        css = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
        for rule in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
            sel = rule.group(1).strip()
            if sel.startswith("@") or not sel:
                continue
            out.append((sel, rule.group(2), base + css[: rule.start()].count("\n")))
    return out


def check(path: Path) -> tuple[list[str], list[str]]:
    html = path.read_text(encoding="utf-8")
    doc = Doc()
    doc.feed(html)
    doc.close()

    errors: list[str] = []
    warns: list[str] = []

    for tag, line in doc.unclosed:
        errors.append(f"{path.name}:{line}  unclosed <{tag}>")
    for tag, line in doc.mismatched:
        errors.append(f"{path.name}:{line}  mismatched </{tag}>")

    rules = css_blocks(html)
    defined: set[str] = set()

    for sel, body, line in rules:
        for cls in re.findall(r"\.([A-Za-z0-9_-]+)", sel):
            defined.add(cls)

        hits = [(p, v) for p, v in BOX_PROPS.findall(body) if _is_significant(p, v)]
        if not hits:
            continue
        disp = DISPLAY.search(body)
        if disp and disp.group(1).lower() not in {"inline"}:
            continue

        # Which elements does this selector actually hit?
        targets: set[str] = set()
        for part in sel.split(","):
            part = part.strip().split()[-1] if part.strip() else ""
            tag_m = re.match(r"^([a-z]+[a-z0-9]*)", part)
            cls_m = re.findall(r"\.([A-Za-z0-9_-]+)", part)
            if tag_m and tag_m.group(1) in INLINE_BY_DEFAULT:
                targets.add(tag_m.group(1))
            for cls in cls_m:
                targets |= {t for t in doc.tag_classes.get(cls, set())
                            if t in INLINE_BY_DEFAULT}
        if targets:
            errors.append(
                f"{path.name}:{line}  `{sel}` sets box properties on "
                f"inline-by-default {sorted(targets)} without `display` — padding "
                f"will not expand the line box"
            )

    for cls in sorted(doc.classes - defined):
        warns.append(f"{path.name}  class '{cls}' used in markup but has no CSS rule")

    for attr, ref, line in doc.refs:
        if ref.startswith(("http://", "https://", "mailto:", "#", "data:")):
            continue
        target = (path.parent / ref).resolve()
        if not target.exists() and not (target / "index.html").exists():
            errors.append(f"{path.name}:{line}  {attr}=\"{ref}\" does not resolve")

    for ref in set(re.findall(r"getElementById\(\s*[\"']([^\"']+)[\"']", html)):
        if ref not in doc.ids:
            errors.append(f"{path.name}  getElementById('{ref}') — no such id in markup")

    for line, for_attr, nests_input in doc.labels:
        if for_attr and nests_input:
            errors.append(
                f"{path.name}:{line}  <label for=\"{for_attr}\"> also nests its input "
                f"— double association fires the picker twice"
            )
        if for_attr and for_attr not in doc.ids:
            errors.append(f"{path.name}:{line}  <label for=\"{for_attr}\"> — no such id")

    return errors, warns


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parents[1]
    paths = [Path(a) for a in argv] or sorted(root.rglob("*.html"))
    all_err: list[str] = []
    all_warn: list[str] = []

    for p in paths:
        errors, warns = check(p)
        all_err += errors
        all_warn += warns
        mark = "FAIL" if errors else "ok  "
        print(f"  [{mark}] {p.relative_to(root) if root in p.parents else p}"
              f"  ({len(errors)} error, {len(warns)} warn)")

    for w in all_warn:
        print(f"  WARN  {w}")
    for e in all_err:
        print(f"  ERROR {e}")

    print(f"\n{len(paths)} page(s), {len(all_err)} error(s), {len(all_warn)} warning(s)")
    return 1 if all_err else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

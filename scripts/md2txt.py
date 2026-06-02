#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["markdown-it-py"]
# ///
"""
Convert LAIA.md to LAIA.txt — plain text, 80 columns.

Usage:
    python3 scripts/md2txt.py LAIA.md LAIA.txt
    python3 scripts/md2txt.py LAIA.md > LAIA.txt
"""

import sys
import textwrap

from markdown_it import MarkdownIt
from markdown_it.tree import SyntaxTreeNode


def render_inline(node: SyntaxTreeNode) -> str:
    """Render an inline SyntaxTreeNode to plain text."""
    parts: list[str] = []
    for child in node.children:
        t = child.type
        if t == "text":
            parts.append(child.content)
        elif t == "code_inline":
            parts.append(child.content)
        elif t in ("strong", "em", "s"):
            parts.append(render_inline(child))
        elif t == "softbreak":
            parts.append(" ")
        elif t == "hardbreak":
            parts.append("\n")
        elif t == "link_open":
            pass
        elif t == "link_close":
            pass
        elif t == "image":
            parts.append(child.content or "")
        elif t == "html_inline":
            if not child.content.startswith("<!--"):
                parts.append(child.content)
        else:
            parts.append(child.content or "")
    return "".join(parts)


def convert(md: str) -> str:
    parser = MarkdownIt("default", {"linkify": False, "typographer": False})
    parser.enable(["table"])
    tokens = parser.parse(md)
    root = SyntaxTreeNode(tokens)

    out: list[str] = []
    list_stack: list[str] = []

    def blank():
        """Add a blank line if the last output line is not already blank."""
        if not out:
            return
        if out[-1] == "":
            return
        out.append("")

    def enter(node: SyntaxTreeNode):
        nonlocal list_stack
        t = node.type

        # ── Headings ──────────────────────────────────────
        if t == "heading":
            level = int(node.tag[1]) if node.tag.startswith("h") else 2
            inline_child = _first_inline(node)
            text = render_inline(inline_child) if inline_child else ""
            blank()
            out.append(text)
            if level <= 2:
                blank()

        # ── Paragraphs ────────────────────────────────────
        elif t == "paragraph":
            inline_child = _first_inline(node)
            if inline_child:
                text = render_inline(inline_child)
                if text.strip():
                    segments = text.split("\n")
                    wrapped = []
                    for seg in segments:
                        w = textwrap.fill(seg, width=80)
                        if w:
                            wrapped.append(w)
                    text_out = "\n".join(wrapped)
                    if list_stack:
                        prefix = list_stack[-1]
                        avail = 80 - len(prefix)
                        if avail >= 20:
                            text_out = textwrap.fill(
                                text_out.replace("\n", " "),
                                width=80,
                                initial_indent=prefix,
                                subsequent_indent=prefix,
                            )
                        else:
                            text_out = prefix + text_out
                    out.append(text_out)
            blank()

        # ── Fenced code blocks ───────────────────────────
        elif t == "fence":
            content = node.content.rstrip("\n")
            if content:
                blank()
                for line in content.split("\n"):
                    out.append("    " + line)
                blank()

        # ── Indented code blocks ─────────────────────────
        elif t == "code_block":
            content = node.content.rstrip("\n")
            if content:
                blank()
                for line in content.split("\n"):
                    out.append("    " + line)
                blank()

        # ── Horizontal rules ─────────────────────────────
        elif t == "hr":
            blank()
            out.append("-" * 72)
            blank()

        # ── Tables ───────────────────────────────────────
        elif t == "table":
            blank()
            rows = []
            for child in node.children:
                if child.type in ("thead", "tbody"):
                    for row in child.children:
                        if row.type == "tr":
                            cells = []
                            for cell in row.children:
                                if cell.type in ("th", "td"):
                                    cells.append(render_inline(cell))
                            if cells:
                                rows.append("  " + " | ".join(cells))
            out.extend(rows)
            blank()

    def exit(node: SyntaxTreeNode):
        nonlocal list_stack
        t = node.type
        if t == "bullet_list" or t == "ordered_list":
            if list_stack:
                list_stack.pop()
            blank()
        elif t == "blockquote":
            blank()

    def walk(node: SyntaxTreeNode):
        enter(node)
        for child in node.children:
            walk(child)
        exit(node)

    walk(root)
    return "\n".join(out)


def _first_inline(node: SyntaxTreeNode) -> SyntaxTreeNode | None:
    for child in node.children:
        if child.type == "inline":
            return child
    return None


def main():
    if len(sys.argv) >= 3:
        with open(sys.argv[1]) as f:
            md = f.read()
        result = convert(md)
        with open(sys.argv[2], "w") as f:
            f.write(result)
            if not result.endswith("\n"):
                f.write("\n")
    elif len(sys.argv) == 2:
        with open(sys.argv[1]) as f:
            md = f.read()
        result = convert(md)
        sys.stdout.write(result)
        if not result.endswith("\n"):
            sys.stdout.write("\n")
    else:
        md = sys.stdin.read()
        result = convert(md)
        sys.stdout.write(result)
        if not result.endswith("\n"):
            sys.stdout.write("\n")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Convert a PDF to Markdown with poppler's ``pdftohtml`` XML output.

The converter is intentionally dependency-free (Python standard library plus
poppler's ``pdftohtml``).  It reconstructs lines from positioned
text spans, drops running headers/footers and common web-print widgets, and
keeps headings, paragraphs, lists, inline bold and simple tables.

Examples
--------
    python3 pdf_to_md.py document.pdf -o document.md
    python3 pdf_to_md.py document.pdf --profile google-ai -o result.md
    python3 pdf_to_md.py document.pdf --drop-regex '^Page \\d+$' \\
        --replace 'teh=the'

Run ``python3 pdf_to_md.py --help`` for all options.
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from statistics import median
from typing import Iterable, Sequence


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Run:
    """A piece of text with inline formatting."""

    text: str
    bold: bool = False
    italic: bool = False
    href: str | None = None


@dataclass
class Span:
    """A positioned text span as reported by pdftohtml."""

    top: float
    left: float
    width: float
    height: float
    size: float
    runs: list[Run]


@dataclass
class Cell:
    """A horizontal group of spans inside a line (a potential table cell)."""

    left: float
    right: float
    text: str


@dataclass
class Line:
    page: int
    top: float
    left: float
    right: float
    size: float
    runs: list[Run]
    text: str
    cells: list[Cell] = field(default_factory=list)
    kind: str = "text"  # text | timestamp | dropped | table
    is_question: bool = False
    table_id: int | None = None

    @property
    def bottom(self) -> float:
        return self.top + self.size

    @property
    def marked(self) -> str:
        return runs_to_marked(self.runs)


@dataclass
class Table:
    columns: list[float]
    rows: list[list[str]]


@dataclass
class Block:
    kind: str  # para | list | heading | question | table
    lines: list[Line] = field(default_factory=list)
    left: float = 0.0
    size: float = 0.0
    level: int = 0
    ordered: bool = False
    number: str = ""
    table: Table | None = None
    text: str = ""
    is_question: bool = False
    question_level: int = 2


# ---------------------------------------------------------------------------
# PDF / XML parsing
# ---------------------------------------------------------------------------


class _RunParser(HTMLParser):
    """Parse the small HTML subset used inside pdftohtml ``<text>`` nodes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.runs: list[Run] = []
        self._bold = 0
        self._italic = 0
        self._href: str | None = None
        self._href_stack: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("b", "strong"):
            self._bold += 1
        elif tag in ("i", "em"):
            self._italic += 1
        elif tag == "a":
            self._href_stack.append(self._href)
            self._href = dict(attrs).get("href")
        elif tag == "br":
            self._add("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("b", "strong"):
            self._bold = max(0, self._bold - 1)
        elif tag in ("i", "em"):
            self._italic = max(0, self._italic - 1)
        elif tag == "a":
            self._href = self._href_stack.pop() if self._href_stack else None

    def handle_data(self, data: str) -> None:
        self._add(data)

    def _add(self, text: str) -> None:
        if not text:
            return
        bold, italic, href = self._bold > 0, self._italic > 0, self._href
        if (
            self.runs
            and self.runs[-1].bold == bold
            and self.runs[-1].italic == italic
            and self.runs[-1].href == href
        ):
            self.runs[-1].text += text
        else:
            self.runs.append(Run(text, bold, italic, href))


def _parse_runs(fragment: str) -> list[Run]:
    parser = _RunParser()
    parser.feed(html.unescape(fragment))
    parser.close()
    return parser.runs


def _run_pdftohtml(pdf: str) -> str:
    exe = shutil.which("pdftohtml")
    if not exe:
        raise SystemExit(
            "pdftohtml was not found. Install poppler-utils "
            "(Debian/Ubuntu: sudo apt install poppler-utils)."
        )
    cmd = [exe, "-xml", "-stdout", "-i", "-q", pdf]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        detail = (proc.stderr or proc.stdout or "").strip()
        raise SystemExit(f"pdftohtml failed: {detail}")
    return proc.stdout


def parse_pdf(pdf: str) -> tuple[list[Line], dict[int, float]]:
    xml = _run_pdftohtml(pdf)
    fonts: dict[str, float] = {}
    for m in re.finditer(
        r'<fontspec id="(\d+)" size="([\d.]+)"', xml
    ):
        fonts[m.group(1)] = float(m.group(2))

    pages = list(re.finditer(r"<page\b([^>]*)>(.*?)</page>", xml, re.S))
    heights: dict[int, float] = {}
    lines: list[Line] = []

    for page_match in pages:
        attrs = page_match.group(1)
        body = page_match.group(2)
        number_m = re.search(r'number="(\d+)"', attrs)
        height_m = re.search(r'height="([\d.]+)"', attrs)
        page = int(number_m.group(1)) if number_m else len(heights) + 1
        heights[page] = float(height_m.group(1)) if height_m else 842.0

        spans: list[Span] = []
        for m in re.finditer(
            r'<text top="([\d.]+)" left="([\d.]+)" width="([\d.]+)" '
            r'height="([\d.]+)" font="(\d+)">(.*?)</text>',
            body,
            re.S,
        ):
            top, left, width, height = map(float, m.group(1, 2, 3, 4))
            size = fonts.get(m.group(5), height)
            spans.append(
                Span(top, left, width, height, size, _parse_runs(m.group(6)))
            )

        lines.extend(_assemble_lines(spans, page))

    return lines, heights


def _assemble_lines(spans: Sequence[Span], page: int) -> list[Line]:
    if not spans:
        return []

    # Group spans that share a baseline.  A tolerance of 6 px copes with the
    # small vertical offsets of superscripts and mixed font sizes.
    ordered = sorted(spans, key=lambda s: (s.top, s.left))
    clusters: list[list[Span]] = []
    cluster_top: list[float] = []
    for span in ordered:
        placed = False
        for idx, top in enumerate(cluster_top):
            if abs(span.top - top) <= 6:
                clusters[idx].append(span)
                placed = True
                break
        if not placed:
            clusters.append([span])
            cluster_top.append(span.top)

    lines: list[Line] = []
    for cluster in clusters:
        cluster.sort(key=lambda s: s.left)
        left = min(s.left for s in cluster)
        right = max(s.left + s.width for s in cluster)
        top = min(s.top for s in cluster)

        weighted_sizes: list[float] = []
        for s in cluster:
            n = max(1, len("".join(r.text for r in s.runs).strip()))
            weighted_sizes.extend([s.size] * n)
        size = median(weighted_sizes)

        runs: list[Run] = []
        prev_right: float | None = None
        for span in cluster:
            span_text = "".join(r.text for r in span.runs)
            if prev_right is not None and span.left - prev_right > 1.5:
                if (
                    runs
                    and not runs[-1].text.endswith((" ", "\n"))
                    and not span_text.startswith((" ", "\n"))
                ):
                    runs.append(Run(" "))
            runs.extend(span.runs)
            prev_right = span.left + span.width

        text = _norm("".join(r.text for r in runs))
        cells = _line_cells(cluster)
        if not text:
            continue
        lines.append(
            Line(
                page=page,
                top=top,
                left=left,
                right=right,
                size=size,
                runs=runs,
                text=text,
                cells=cells,
            )
        )
    return lines


def _line_cells(cluster: Sequence[Span], gap: float = 25.0) -> list[Cell]:
    cells: list[Cell] = []
    current: list[Span] = []
    for span in cluster:
        if not current:
            current = [span]
            continue
        prev_right = current[-1].left + current[-1].width
        if span.left - prev_right > gap:
            cells.append(_make_cell(current))
            current = [span]
        else:
            current.append(span)
    if current:
        cells.append(_make_cell(current))
    return [c for c in cells if c.text]


def _make_cell(spans: Sequence[Span]) -> Cell:
    left = min(s.left for s in spans)
    right = max(s.left + s.width for s in spans)
    parts: list[str] = []
    prev_right: float | None = None
    for span in spans:
        text = "".join(r.text for r in span.runs)
        if prev_right is not None and span.left - prev_right > 1.5:
            if parts and not parts[-1].endswith(" ") and not text.startswith(" "):
                parts.append(" ")
        parts.append(text)
        prev_right = span.left + span.width
    return Cell(left, right, _norm("".join(parts)))


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _norm_run(text: str) -> str:
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", text)


def _ends_sentence(text: str) -> bool:
    return bool(re.search(r"[.!?][\"»)]?\s*$", text.strip()))


def _starts_upper(text: str) -> bool:
    stripped = text.lstrip()
    return bool(stripped) and stripped[0].isupper()


def runs_to_marked(runs: Sequence[Run]) -> str:
    merged: list[Run] = []
    for run in runs:
        if not run.text:
            continue
        if (
            merged
            and merged[-1].bold == run.bold
            and merged[-1].italic == run.italic
            and merged[-1].href == run.href
        ):
            merged[-1].text += run.text
        else:
            merged.append(Run(run.text, run.bold, run.italic, run.href))

    pieces: list[str] = []
    for run in merged:
        text = _norm_run(run.text)
        if not text:
            continue
        lead = text[: len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()) :]
        core = text.strip()
        if not core:
            pieces.append(" ")
            continue
        if run.href:
            core = f"[{core}]({run.href})"
        if run.bold:
            core = f"**{core}**"
        if run.italic:
            core = f"*{core}*"
        pieces.append(lead + core + trail)

    result = "".join(pieces)
    return _clean_inline(result)


def _clean_inline(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([«(])\s+", r"\1", text)
    text = re.sub(r"\s+([»)])", r"\1", text)
    text = re.sub(r"(?<=\w)/\s+(?=\w)", "/", text)
    text = re.sub(r"\*\*\s*\*\*", " ", text)
    # Merge separately bolded words: **a** **b** -> **a b**
    previous = None
    while previous != text:
        previous = text
        text = re.sub(
            r"\*\*([^*]+)\*\*\s+\*\*([^*]+)\*\*", r"**\1 \2**", text
        )
    # Merge separately italicised words/phrases: *a*, *b* -> *a, b*
    previous = None
    while previous != text:
        previous = text
        text = re.sub(
            r"(?<!\*)\*([^*\n]+)\*([\s,.;:!?—–-]+)(?<!\*)\*([^*\n]+)\*(?!\*)",
            r"*\1\2\3*",
            text,
        )
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _join_line_texts(lines: Sequence[Line]) -> str:
    parts: list[str] = []
    for line in lines:
        text = line.marked.strip()
        if not text:
            continue
        if parts:
            prev = parts[-1]
            if prev.endswith("-") and text[:1].islower():
                parts[-1] = prev + text
                continue
            parts[-1] = prev + " " + text
        else:
            parts.append(text)
    return _clean_inline("".join(parts))


# ---------------------------------------------------------------------------
# Cleaning: headers, footers, widgets
# ---------------------------------------------------------------------------


GOOGLE_WIDGET_PATTERNS = [
    re.compile(r"^AI Mode\b"),
    re.compile(r"^(Ask anything\s*)+$", re.I),
    re.compile(r"^\d{1,2}:\d{2}$"),
    re.compile(r"^This is for informational purposes only"),
    re.compile(r"^professional\. AI responses may include mistakes"),
    re.compile(r"^Learn more$", re.I),
    re.compile(r"^Quick questions if you have time:?$", re.I),
]


def _normalise_for_repeat(text: str) -> str:
    text = re.sub(r"https?://\S+", "URL", text.lower())
    text = re.sub(r"\d+", "#", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def clean_lines(
    lines: list[Line],
    heights: dict[int, float],
    profile: str,
    drop_regexes: Sequence[re.Pattern[str]],
) -> tuple[list[Line], list[str]]:
    """Drop running headers/footers, page numbers and profile widgets."""

    n_pages = max(heights) if heights else 1
    page_sets: dict[str, set[int]] = defaultdict(set)
    for line in lines:
        page_sets[_normalise_for_repeat(line.text)].add(line.page)

    repeated = {
        key
        for key, pages in page_sets.items()
        if key and len(pages) >= max(2, int(0.6 * n_pages) + 1)
    }

    disclaimers: list[str] = []
    kept: list[Line] = []
    in_quick_questions = False

    for line in lines:
        text = line.text.strip()
        if not text:
            continue

        # Timestamps in the Google AI profile are question markers, even when
        # they happen to sit in the page margins.
        if profile == "google-ai" and re.fullmatch(r"\d{1,2}:\d{2}", text):
            line.kind = "timestamp"
            kept.append(line)
            continue

        # Running header/footer: repeated text in the page margins.
        key = _normalise_for_repeat(text)
        page_h = heights.get(line.page, 842.0)
        in_margin = line.top < page_h * 0.12 or line.top > page_h * 0.88
        if key in repeated and in_margin:
            continue

        # Page numbers such as "12/22" or "12".
        if re.fullmatch(r"\d+\s*(?:/\s*\d+)?", text):
            continue

        if any(rx.search(text) for rx in drop_regexes):
            continue

        if profile == "google-ai":
            if in_quick_questions:
                if text.startswith("This is for informational purposes only"):
                    in_quick_questions = False
                continue

            if re.match(r"^Quick questions if you have time:?$", text, re.I):
                in_quick_questions = True
                continue

            if any(rx.search(text) for rx in GOOGLE_WIDGET_PATTERNS):
                if text.startswith("This is for informational purposes only"):
                    disclaimers.append(text)
                elif text.startswith("professional. AI responses may include mistakes"):
                    disclaimers.append(text)
                elif re.fullmatch(r"\d{1,2}:\d{2}", text):
                    line.kind = "timestamp"
                    kept.append(line)
                continue

        kept.append(line)

    return kept, disclaimers


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _cluster_positions(values: Iterable[float], tolerance: float = 20.0) -> list[float]:
    clusters: list[list[float]] = []
    for value in sorted(values):
        if clusters and value - clusters[-1][-1] <= tolerance:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return [median(c) for c in clusters]


def find_tables(
    lines: list[Line],
    body_size: float,
    min_columns: int = 3,
) -> list[Table]:
    tables: list[Table] = []
    used: set[int] = set()

    for i, line in enumerate(lines):
        if i in used or len(line.cells) < min_columns:
            continue
        if line.size > body_size + 1.5:
            continue

        # Grow upwards: include narrow header cells that sit above the anchor.
        start = i
        first = lines[start]
        anchor_cols = _cluster_positions(c.left for c in line.cells)
        while start > 0:
            prev = lines[start - 1]
            if prev.page != line.page:
                break
            if first.top - prev.top > 35 or prev.size > body_size + 1.5:
                break
            if not any(
                min(abs(c.left - col) for col in anchor_cols) <= 25
                for c in prev.cells
            ):
                break
            start -= 1
            first = prev

        # Grow downwards while rows keep the same column geometry.
        end = i
        last = lines[i]
        while end + 1 < len(lines):
            nxt = lines[end + 1]
            if nxt.page != last.page:
                break
            if nxt.top - last.top > 58 or nxt.size > body_size + 1.5:
                break
            anchor_cols = _cluster_positions(
                c.left for c in lines[i].cells
            )
            if not any(
                min(abs(c.left - col) for col in anchor_cols) <= 25
                for c in nxt.cells
            ):
                break
            end += 1
            last = nxt

        region = lines[start : end + 1]
        if len(region) < 2:
            continue

        # Build row bands: lines separated by a larger vertical gap.
        bands: list[list[Line]] = []
        for ln in region:
            if not bands:
                bands.append([ln])
            elif ln.top - bands[-1][-1].top > 32:
                bands.append([ln])
            else:
                bands[-1].append(ln)
        if len(bands) < 2:
            continue

        columns = _cluster_positions(
            c.left for ln in region for c in ln.cells
        )
        # Keep only columns that occur in at least two lines.
        counts = Counter()
        for ln in region:
            for c in ln.cells:
                if columns:
                    col = min(columns, key=lambda x: abs(x - c.left))
                    if abs(col - c.left) <= 30:
                        counts[col] += 1
        columns = sorted(col for col in columns if counts[col] >= 2)
        if len(columns) < min_columns:
            continue

        rows: list[list[str]] = []
        for band in bands:
            cells_by_col: dict[int, list[str]] = {idx: [] for idx in range(len(columns))}
            for ln in band:
                for cell in ln.cells:
                    col = min(
                        range(len(columns)),
                        key=lambda idx: abs(columns[idx] - cell.left),
                    )
                    if abs(columns[col] - cell.left) <= 30:
                        cells_by_col[col].append(cell.text)
            row = [_norm(" ".join(cells_by_col[idx])) for idx in range(len(columns))]
            if any(row):
                rows.append(row)

        if len(rows) < 2 or sum(1 for cell in rows[0] if cell) < min_columns:
            continue

        tables.append(Table(columns=columns, rows=rows))
        table_id = len(tables) - 1
        for ln in region:
            used.add(lines.index(ln))
            ln.kind = "table"
            ln.table_id = table_id

    return tables


# ---------------------------------------------------------------------------
# Blocks
# ---------------------------------------------------------------------------


def _new_block(line: Line, body_left: float, indent_step: float) -> Block:
    kind = "para"
    starts_ordered = bool(
        re.match(r"^(\*\*)?\d+[.)](\*\*)?\s+", line.text)
    )
    if line.size > 20:
        kind = "heading"
    elif (
        starts_ordered
        or line.left > body_left + indent_step * 0.4
    ) and not _all_italic([line]):
        kind = "list"
    level = max(1, round((line.left - body_left) / indent_step)) if kind == "list" else 0
    return Block(kind=kind, lines=[line], left=line.left, size=line.size, level=level)


def build_blocks(
    lines: list[Line],
    body_left: float,
    body_size: float,
    line_height: float,
    indent_step: float,
) -> list[Block]:
    blocks: list[Block] = []
    current: Block | None = None
    prev: Line | None = None

    for line in lines:
        if line.kind in ("timestamp", "dropped"):
            if line.kind == "timestamp":
                if current is not None:
                    blocks.append(current)
                    current = None
                if blocks:
                    blocks[-1].is_question = True
            continue
        if line.kind == "table":
            if current is not None:
                blocks.append(current)
                current = None
            if not blocks or blocks[-1].kind != "table" or blocks[-1].lines[0].table_id != line.table_id:
                blocks.append(
                    Block(kind="table", lines=[line], left=line.left, size=line.size)
                )
            else:
                blocks[-1].lines.append(line)
            prev = line
            continue

        if current is None:
            current = _new_block(line, body_left, indent_step)
            prev = line
            continue

        same_page = line.page == prev.page if prev else True
        gap = line.top - prev.top if (prev and same_page) else 0.0
        continuation = False
        starts_ordered = bool(
            re.match(r"^(\*\*)?\d+[.)](\*\*)?\s+", line.text)
        )
        starts_bold_lead = bool(re.match(r"^\*\*[^*]+:\*\*", line.text))
        cross_page_break = (
            not same_page
            and _ends_sentence(current.lines[-1].text)
            and _starts_upper(line.text)
        )

        if (
            not (starts_ordered and current.kind == "list")
            and not (starts_bold_lead and current.kind == "list")
            and not cross_page_break
            and current.kind in ("para", "list", "heading")
            and abs(line.size - current.size) < 1.5
            and (gap <= line_height * 1.4 or not same_page)
            and (
                current.kind != "heading"
                or line.size > body_size + 1.5
            )
        ):
            if abs(line.left - current.left) <= 6:
                continuation = True
            elif abs(line.left - (current.left + indent_step)) <= 6:
                last_text = current.lines[-1].text.rstrip()
                if not _ends_sentence(last_text):
                    if not (
                        last_text.endswith(":")
                        and _starts_upper(line.text)
                    ):
                        continuation = True

        if continuation:
            current.lines.append(line)
        else:
            blocks.append(current)
            current = _new_block(line, body_left, indent_step)
        prev = line

    if current is not None:
        blocks.append(current)

    # Mark questions after building: a timestamp follows its question.
    for idx, block in enumerate(blocks):
        if not block.is_question:
            continue
        block.kind = "question"
        block.question_level = 1 if not any(
            b.kind == "question" for b in blocks[:idx]
        ) else 2

    return blocks


def _block_text(block: Block) -> str:
    return _join_line_texts(block.lines)


def _strip_emphasis(text: str) -> str:
    return re.sub(r"\*+", "", text).strip()


def _all_bold(lines: Sequence[Line]) -> bool:
    saw_text = False
    for line in lines:
        for run in line.runs:
            if not run.text.strip():
                continue
            saw_text = True
            if not run.bold:
                return False
    return saw_text


def _all_italic(lines: Sequence[Line]) -> bool:
    saw_text = False
    for line in lines:
        for run in line.runs:
            # Punctuation between italic words is often tagged as regular
            # text by pdftohtml, so only letter-bearing runs matter here.
            if not any(ch.isalpha() for ch in run.text):
                continue
            saw_text = True
            if not run.italic:
                return False
    return saw_text


def _list_marker(text: str) -> tuple[bool, str, str]:
    """Split an ordered-list marker from the text, if present."""
    match = re.match(r"^(\*\*)?(\d+)[.)](\*\*)?\s+(.*)$", text, re.S)
    if match:
        return True, match.group(2), match.group(4).strip()
    return False, "", text.strip()


def render(blocks: list[Block], disclaimers: Sequence[str]) -> str:
    out: list[str] = []
    prev_kind: str | None = None
    list_offset = 0

    for block in blocks:
        if block.kind == "table":
            table_id = block.lines[0].table_id
            table = block.table
            if table is None:
                continue
            header = table.rows[0]
            body = table.rows[1:]
            md = [
                "| " + " | ".join(_escape_table(c) for c in header) + " |",
                "| " + " | ".join("---" for _ in header) + " |",
            ]
            for row in body:
                md.append("| " + " | ".join(_escape_table(c) for c in row) + " |")
            out.append("\n".join(md))
            prev_kind = "table"
            continue

        text = _block_text(block)
        if not text:
            continue

        emitted_kind = block.kind
        if block.kind == "question":
            level = block.question_level
            text = _strip_emphasis(text)
            text = text[0].upper() + text[1:] if text else text
            if level == 1 and not text.endswith(("?", ".", "!")):
                text += "?"
            out.append(f"{'#' * level} {text}")
        elif block.kind == "heading":
            text = _strip_emphasis(text)
            text = text[0].upper() + text[1:] if text else text
            out.append(f"### {text}")
        elif block.kind in ("para", "list") and _all_bold(block.lines) and len(text) < 160:
            text = _strip_emphasis(text)
            text = text[0].upper() + text[1:] if text else text
            out.append(f"#### {text}")
            emitted_kind = "heading"
        elif block.kind == "list":
            ordered, number, rest = _list_marker(text)
            # A list that does not follow another list item starts a new run.
            # If it is visually nested but has no parent list item, shift the
            # whole run up so Markdown does not treat it as a code block.
            if prev_kind != "list":
                list_offset = max(0, block.level - 1)
            level = max(1, block.level - list_offset)
            indent = "    " * max(0, level - 1)
            if ordered:
                out.append(f"{indent}{number}. {rest}")
            else:
                out.append(f"{indent}- {rest}")
        else:
            out.append(text)

        prev_kind = emitted_kind

    # Keep one copy of the medical/legal disclaimer.
    if disclaimers:
        seen: set[str] = set()
        merged: list[str] = []
        for part in disclaimers:
            part = part.strip()
            if part and part not in seen:
                seen.add(part)
                merged.append(part)
        text = " ".join(merged)
        text = text.replace(" Learn more", "")
        out.append(f"> {text}")

    # Separate blocks with blank lines; keep consecutive list items tight.
    result: list[str] = []
    for piece in out:
        if not result:
            result.append(piece)
            continue
        is_list = piece.lstrip().startswith(("- ", "1. ", "2. ", "3. ", "4. ", "5. "))
        prev_is_list = result[-1].lstrip().startswith(
            ("- ", "1. ", "2. ", "3. ", "4. ", "5. ")
        )
        if is_list and prev_is_list:
            result.append(piece)
        else:
            result.append("")
            result.append(piece)
    return "\n".join(result).strip() + "\n"


def _escape_table(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def convert(
    pdf: str,
    output: str,
    profile: str = "auto",
    drop_regexes: Sequence[str] = (),
    replacements: Sequence[str] = (),
    min_table_columns: int = 3,
) -> None:
    lines, heights = parse_pdf(pdf)

    if profile == "auto":
        joined = " ".join(line.text for line in lines[:40])
        profile = "google-ai" if re.search(r"AI Mode|Google Search", joined) else "generic"

    compiled_drops = [re.compile(rx) for rx in drop_regexes]
    lines, disclaimers = clean_lines(lines, heights, profile, compiled_drops)
    lines.sort(key=lambda ln: (ln.page, ln.top, ln.left))

    # Determine the dominant body geometry.
    lefts = Counter(round(line.left) for line in lines if line.kind == "text")
    body_left = float(lefts.most_common(1)[0][0]) if lefts else 0.0

    size_weights: Counter[float] = Counter()
    for line in lines:
        if line.kind == "text":
            size_weights[round(line.size, 1)] += len(line.text)
    body_size = size_weights.most_common(1)[0][0] if size_weights else 12.0

    gaps = [
        b.top - a.top
        for a, b in zip(lines, lines[1:])
        if a.page == b.page and 0 < b.top - a.top < 80
    ]
    line_height = median(gaps) if gaps else body_size * 1.5
    indent_step = 25.0

    tables = find_tables(lines, body_size, min_columns=min_table_columns)
    blocks = build_blocks(lines, body_left, body_size, line_height, indent_step)

    # Attach tables to their blocks.
    for block in blocks:
        if block.kind == "table":
            table_id = block.lines[0].table_id
            if table_id is not None and 0 <= table_id < len(tables):
                block.table = tables[table_id]

    text = render(blocks, disclaimers)

    for old, new in (item.split("=", 1) for item in replacements if "=" in item):
        text = text.replace(old, new)

    with open(output, "w", encoding="utf-8") as handle:
        handle.write(text)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a PDF to Markdown using poppler's pdftohtml.",
    )
    parser.add_argument("pdf", help="input PDF file")
    parser.add_argument("-o", "--output", help="output Markdown file (default: PDF name with .md)")
    parser.add_argument(
        "--profile",
        choices=("auto", "generic", "google-ai"),
        default="auto",
        help="cleaning profile (default: auto-detect)",
    )
    parser.add_argument(
        "--drop-regex",
        action="append",
        default=[],
        metavar="REGEX",
        help="drop any line matching REGEX (repeatable)",
    )
    parser.add_argument(
        "--replace",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="replace text in the generated Markdown (repeatable)",
    )
    parser.add_argument(
        "--min-table-columns",
        type=int,
        default=3,
        help="minimum number of columns for table detection (default: 3)",
    )
    args = parser.parse_args(argv)

    output = args.output or re.sub(r"\.[Pp][Dd][Ff]$", ".md", args.pdf)
    convert(
        args.pdf,
        output,
        profile=args.profile,
        drop_regexes=args.drop_regex,
        replacements=args.replace,
        min_table_columns=args.min_table_columns,
    )
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

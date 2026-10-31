# pdf_to_md.py

A small, dependency-free PDF → Markdown converter built around poppler's
`pdftohtml` XML output. It is designed for text-based PDFs such as browser
printouts, articles, and chat transcripts: it removes running headers/footers
and web UI chrome, then reconstructs headings, paragraphs, lists, inline
formatting, and simple tables.

The converter uses only the Python standard library plus the poppler command
line tools. No `pandoc`, no Python packages, no network access.

## Features

- Rebuilds lines from positioned text spans, so oddly spaced PDF text is
  reassembled into readable lines.
- Removes repeated page headers/footers, page numbers, and common web-print
  widgets (Google AI Mode / Search printouts are supported out of the box).
- Detects headings by font size, lists by indentation and ordered markers, and
  fully bold short blocks as sub-headings.
- Preserves inline **bold**, *italic*, and links.
- Reconstructs simple tables as GitHub-flavoured Markdown pipe tables.
- Keeps one copy of a repeated medical/legal disclaimer as a blockquote.
- Supports per-line regex dropping and post-generation text replacements.
- Auto-detects the cleaning profile, with manual overrides.

## Requirements

- Python 3.9 or newer.
- [poppler-utils](https://poppler.freedesktop.org/): specifically `pdftohtml`.

Install poppler:

```bash
# Debian / Ubuntu
sudo apt install poppler-utils

# Fedora
sudo dnf install poppler-utils

# macOS (Homebrew)
brew install poppler
```

## Quick start

```bash
python3 pdf_to_md.py document.pdf
```

This writes `document.md` next to the input file. To choose the output path:

```bash
python3 pdf_to_md.py document.pdf -o output.md
```

## Options

| Option | Description |
| --- | --- |
| `pdf` | Input PDF file (required). |
| `-o`, `--output PATH` | Output Markdown file. Defaults to the input name with `.md`. |
| `--profile {auto,generic,google-ai}` | Cleaning profile. Default: `auto`. |
| `--drop-regex REGEX` | Drop any extracted line matching the Python regex. Repeatable. |
| `--replace OLD=NEW` | Replace text in the generated Markdown. Repeatable. |
| `--min-table-columns N` | Minimum columns for table detection. Default: `3`. |
| `-h`, `--help` | Show help. |

### Profiles

- **`auto`** (default) — looks for `AI Mode` or `Google Search` in the first
  extracted lines. If found, uses `google-ai`; otherwise uses `generic`.
- **`generic`** — removes repeated text in the top and bottom page margins,
  page numbers, and any user-supplied `--drop-regex` lines. Suitable for
  articles, reports, and most browser printouts.
- **`google-ai`** — everything in `generic`, plus Google UI cleanup:
  - drops `AI Mode`, `Ask anything`, `Learn more`, and the
    `Quick questions` poll;
  - treats `HH:MM` timestamps as user-turn boundaries and turns the preceding
    block into a question heading;
  - keeps a single copy of the informational disclaimer at the end.

### Drop and replace examples

```bash
# Remove lines such as "Page 12" or "12/22"
python3 pdf_to_md.py report.pdf \
  --drop-regex '^Page \d+$' \
  --drop-regex '^\d+/\d+$'

# Fix a recurring source typo in the generated Markdown
python3 pdf_to_md.py document.pdf --replace 'teh=the'

# Multiple replacements
python3 pdf_to_md.py document.pdf \
  --replace 'лт ужина=от ужина' \
  --replace 'Color=Colour'
```

`--drop-regex` is applied to extracted line text before block building.
`--replace` is applied to the final Markdown, so it can also fix text that was
split across lines in the PDF.

## Output mapping

Headings, lists, and tables are produced by both profiles. Question headings
and the closing disclaimer blockquote are specific to the `google-ai` profile.

| PDF structure | Markdown output |
| --- | --- |
| First user turn / document title | `# Heading` |
| Later user turns (Google AI profile) | `## Heading` |
| Large-font section heading | `### Heading` |
| Short fully bold block | `#### Heading` |
| Bulleted item (by indentation) | `- item` |
| Numbered item | `1. item` |
| Nested list | 4-space indented list |
| Simple table with ≥ 3 columns | GFM pipe table |
| Repeated disclaimer | One final `> blockquote` |

## How it works

1. **Parse** — `pdftohtml -xml -stdout -i -q` emits positioned text spans.
   Spans sharing a baseline are clustered into lines; gaps between spans become
   spaces.
2. **Clean** — repeated margin text, page numbers, profile widgets, and
   user-supplied regex matches are dropped. Timestamps are recorded as question
   markers for the Google AI profile.
3. **Detect tables** — lines with consistent column x-positions are grouped into
   rows and bands, then rendered as Markdown tables.
4. **Build blocks** — lines are joined into paragraphs, headings, list items,
   and questions using font size, indentation, spacing, and sentence
   boundaries.
5. **Render** — blocks become Markdown, consecutive list items stay tight, and
   one disclaimer is appended.

## Using it in this repository

The source document in this folder is a Google AI Mode printout. Generate the
Markdown with:

```bash
python3 pdf_to_md.py \
  "требуется ли специальный выход из голодания при 40 часовом еженедельном голодании - Google Search.pdf" \
  -o голодание.md \
  --replace "лт ужина=от ужина"
```

The result is `голодание.md`: 14 user turns, 37 section headings, nested lists,
one comparison table, and a single closing disclaimer.

## Limitations

- **Text-based PDFs only.** Scanned documents without a text layer produce
  little or no output. Run OCR first (for example with `ocrmypdf`) and convert
  the OCR'd PDF.
- **Images are ignored.** The converter passes `-i` to `pdftohtml`; it does not
  extract or describe images.
- **Simple tables only.** Table detection relies on consistent column geometry.
  Merged cells, nested tables, and tables with fewer than
  `--min-table-columns` columns are not reconstructed.
- **Heuristic structure.** Headings, lists, and questions are inferred from
  font size, indentation, and spacing. Unusual layouts may need manual cleanup
  or a custom `--drop-regex` / `--replace` pass.
- **Multi-column pages** can interleave if the columns share baselines. The
  line clustering is tuned for single-column printouts.
- **No Markdown escaping of prose.** Table cells are escaped, but body text is
  emitted as-is. If your source contains Markdown control characters, post-edit
  the output.

## Troubleshooting

**`pdftohtml was not found`**

Install poppler-utils (see Requirements) and make sure `pdftohtml` is on
`PATH`.

**The output is empty or nearly empty**

The PDF probably has no text layer. Check with:

```bash
pdftotext document.pdf - | head
```

If that is empty, OCR the document first.

**A table was not detected**

Table detection needs at least `--min-table-columns` aligned columns and at
least two rows. Try lowering the threshold:

```bash
python3 pdf_to_md.py document.pdf --min-table-columns 2
```

Lowering it too far can turn ordinary indented text into tables, so inspect the
result.

**Headers or footers remain**

Add an explicit drop rule:

```bash
python3 pdf_to_md.py document.pdf --drop-regex '^My Report — Page \d+$'
```

**Words are glued together or split oddly**

This usually comes from the PDF's text encoding rather than the converter. Fix
the recurring cases with `--replace`, or compare against `pdftotext` output to
identify the pattern.

## Development

The script is a single file with no third-party imports. Syntax check:

```bash
python3 -m py_compile pdf_to_md.py
```

Main entry points:

- `parse_pdf()` / `_assemble_lines()` — XML parsing and line reconstruction.
- `clean_lines()` — header, footer, widget, and regex cleanup.
- `find_tables()` — column/row detection.
- `build_blocks()` — paragraph, heading, list, and question grouping.
- `render()` — Markdown generation.
- `convert()` / `main()` — pipeline and CLI.

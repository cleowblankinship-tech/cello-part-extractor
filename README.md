# cello-parts

Pull a single instrument's line out of a dense multi-staff score (piano/vocal/
ensemble arrangements) and re-lay it out as a clean, **large-print, single-line
part** — so a cellist doesn't have to read their line buried between the piano
staves, squint at tiny print, or flip pages constantly.

It works on the *layout* of the PDF, not the music: it finds the staff lines,
groups them into systems, extracts the chosen staff from every system, and
stacks the strips big on fresh pages. No music recognition, no re-typesetting —
fast and reliable for scores where your part is always in the same position.

## Install

```bash
pip install -r requirements.txt
```

## Usage

```bash
# Cello = 2nd staff of each system (Violin / Violoncello / Piano score)
python extract_part.py samples/all_of_me.pdf -o output/all_of_me_cello.pdf

# See exactly what the detector found on a page (writes debug_overlay.png)
python extract_part.py samples/all_of_me.pdf --debug-page 0

# Multi-piece book: only pages 9-21 are the string-quartet arrangement whose
# cello is the 4th of 6 staves
python extract_part.py wedding_book.pdf --pages 9-21 --staff-index 3 -o cello.pdf
```

The debug overlay colours each staff of every system and puts magenta brackets
on the staff that will be extracted. **Always eyeball one page this way when you
try a new piece** — it takes a second and tells you if `--staff-index` is right.

The output keeps the original title block (title / composer / arranger), adds a
part label and page numbers, trims the leading system barline so each line
starts at the clef, and packs the lines full-width with standard spacing. The
vertical crop is **content-aware**: it grows out from the staff through attached
ink — ledger notes, slurs, dynamics — and stops at the whitespace between
staves, so it doesn't clip your notes or pull in the neighbour's lyrics.

Staff detection uses an **adaptive (Otsu) threshold**, so it works on scanned /
photocopied scores where the staff lines are grey rather than solid black, not
just clean born-digital PDFs.

## Key options

| Option | Default | What it does |
| --- | --- | --- |
| `--staff-index N` | `1` | 0-based position of your part within each system. Cello 2nd from top = `1`; bottom staff of a 4-staff system = `3`. |
| `--staves-per-system N` | auto | Staves per system for regular scores. Auto-detected; set it if a page is misgrouped. |
| `--pages A-B` | all | 1-based, inclusive page range to process. Use it for multi-piece books where only one arrangement has your part (e.g. `--pages 9-21`). |
| `--part-label TEXT` | `Cello` | Name shown in the header and running title. |
| `--title TEXT` | from score | Override the running title (otherwise read from the title block). |
| `--gap PT` | `12` | Vertical space between lines. Lower = more lines per page. |
| `--landscape` | off | Landscape output (wider lines, great on a tablet). |
| `--page-size` | `letter` | `letter` or `a4`. |
| `--margin PT` | `40` | Page margin in points. |
| `--no-header` | off | Omit the title block / part label. |
| `--no-page-numbers` | off | Omit page numbers. |
| `--detect-px N` | `1600` | Normalized render width for detection. Only touch if detection misbehaves. |

## How to adapt to a new score

1. Run `--debug-page 0` (and a later page) and open `debug_overlay.png`.
2. The magenta box shows the *exact crop* that will be taken. Check it sits on
   your part and encloses its notes/slurs/dynamics. If it's on the wrong staff,
   change `--staff-index`.
3. Generate. Adjust `--gap` / `--landscape` / `--page-size` for the print size.

## Known limitations

- Assumes your part sits at a **consistent staff position** in every system.
- Does **not** transpose or change clef (treble→bass is a future, harder step
  that needs optical music recognition).
- Purely visual — if the source scan is skewed or very noisy, detection may
  need `--detect-px` tuning.

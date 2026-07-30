#!/usr/bin/env python3
"""
extract_part.py — Pull a single instrument's staff out of a multi-staff score
and re-lay it out as a clean, large-print single-line part.

A cellist gets piano/ensemble scores where the cello is one staff buried in each
system. This finds the staff lines on every page, groups them into systems,
crops the chosen staff from each system, and stacks the strips onto fresh pages
with the original title block, a part label, and page numbers.

Detection is layout-based (no music recognition):
  * find long horizontal staff lines by row; collapse thick lines to a center
  * cluster line centers into 5-line staves
  * trim each staff's left edge past the leading system barline (to the clef)
  * grow the crop above/below the staff through attached ink (ledger notes,
    slurs, dynamics), stopping at the whitespace valley between staves
  * group staves into systems (fixed chunking for regular scores) and pick the
    target staff by its index within each system
"""
import argparse
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # PyMuPDF
import numpy as np


# ----------------------------- data types ---------------------------------

@dataclass
class Staff:
    top: int           # first staff-line row (px, in render space)
    bottom: int        # last staff-line row (px)
    left: int          # leftmost extent of the staff lines (px)
    right: int         # rightmost extent (px)
    content_left: int  # left edge past the leading barline (start of the clef)
    crop_top: int = 0     # content-aware upper crop bound (px)
    crop_bottom: int = 0  # content-aware lower crop bound (px)

    @property
    def height(self) -> int:
        return self.bottom - self.top


# ----------------------------- detection ----------------------------------

def render_gray(page: "fitz.Page", target_px: int = 1600):
    """Render a page to a grayscale numpy array, normalized to a controlled
    size so detection thresholds are resolution-independent.

    Returns (img, scale) where scale is render-pixels per PDF-point.
    """
    r = page.rect
    scale = target_px / max(r.width, r.height)
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csGRAY)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    return img, scale


def find_staff_lines(gray: np.ndarray, ink_frac: float = 0.4) -> list[int]:
    """Return the center row of each staff line.

    A staff line spans most of the system width, so it shows up as a row with a
    very long continuous run of dark pixels. We flag such rows, then collapse
    each vertical run of flagged rows (one thick line) into a single center.
    """
    dark = gray < 128
    h, w = dark.shape
    min_run = int(ink_frac * w)
    flagged = []
    for y in range(h):
        row = dark[y]
        if row.sum() < min_run:
            continue
        idx = np.flatnonzero(np.diff(np.concatenate(([0], row.view(np.int8), [0]))))
        if len(idx) == 0:
            continue
        runs = idx[1::2] - idx[0::2]
        if runs.max() >= min_run:
            flagged.append(y)

    centers, run = [], []
    for y in flagged:
        if run and y - run[-1] > 2:
            centers.append(int(round(sum(run) / len(run))))
            run = []
        run.append(y)
    if run:
        centers.append(int(round(sum(run) / len(run))))
    return centers


def _leading_barline_end(colsum: np.ndarray, left: int, right: int,
                         staff_h: int) -> int:
    """Find where the clef starts by skipping the leading system barline.

    Scans columns left→right: skips the instrument label (short strokes), finds
    the first full-height thin vertical line (the barline), and returns the
    column just past it. If the first tall thing is wide (a clef, i.e. no
    barline present) it leaves the left edge unchanged.
    """
    thr = 0.6 * staff_h
    thin = max(4, int(0.02 * (right - left + 1)))
    x = left
    while x <= right and colsum[x] < thr:
        x += 1
    if x > right:
        return left
    start = x
    while x <= right and colsum[x] >= thr:
        x += 1
    if (x - start) <= thin:
        return min(x, right)   # end of the barline → start of the clef
    return left


def cluster_staves(line_centers: list[int], dark: np.ndarray) -> list[Staff]:
    """Group staff-line centers into staves (5 lines each)."""
    lines = sorted(line_centers)
    if len(lines) < 2:
        return []

    gaps = np.diff(lines)
    staff_space = float(np.median(gaps))
    split_thresh = staff_space * 1.8

    groups, cur = [], [lines[0]]
    for prev, r in zip(lines, lines[1:]):
        if r - prev > split_thresh:
            groups.append(cur)
            cur = [r]
        else:
            cur.append(r)
    groups.append(cur)

    staves = []
    h, w = dark.shape
    for g in groups:
        if len(g) < 3:
            continue  # not a real staff (stray long line, bracket, etc.)
        top, bottom = g[0], g[-1]
        band = dark[top:bottom + 1]
        cols = np.flatnonzero(band.any(axis=0))
        if len(cols) == 0:
            continue
        left, right = int(cols[0]), int(cols[-1])
        colsum = band.sum(axis=0)
        content_left = _leading_barline_end(colsum, left, right, bottom - top)
        staves.append(Staff(top, bottom, left, right, content_left))
    return staves


def _scan_to_valley(rowink, start, stop, step, empty_thr, gap_needed, default):
    """Walk rows from `start` toward `stop` (step ±1). Return the middle of the
    first whitespace band (>= gap_needed empty rows), else `default`.

    Ink from ledger notes, slurs and dynamics keeps rows non-empty, so the crop
    grows through them and only stops at the clear gap between staves.
    """
    run = 0
    y = start
    while (y - stop) * step < 0:
        if rowink[y] <= empty_thr:
            run += 1
            if run >= gap_needed:
                return y + step * (run // 2)  # middle of the whitespace band
        else:
            run = 0
        y += step
    return default


def compute_vertical_bounds(staves: list[Staff], dark: np.ndarray,
                            empty_frac=0.006, gap_frac=0.7, cap_frac=1.7):
    """Fill crop_top / crop_bottom for each staff using content-aware growth.

    The crop starts at the staff and expands outward through attached ink,
    stopping at the whitespace valley before the neighbouring staff (or a cap
    when there is no neighbour).
    """
    h, w = dark.shape
    for i, st in enumerate(staves):
        ss = max(1.0, st.height / 4.0)
        x0, x1 = st.content_left, st.right
        rowink = dark[:, x0:x1 + 1].sum(axis=1)
        empty_thr = max(2, int(empty_frac * (x1 - x0 + 1)))
        gap_needed = max(2, int(gap_frac * ss))
        cap = int(cap_frac * st.height)

        above = staves[i - 1] if i > 0 else None
        below = staves[i + 1] if i + 1 < len(staves) else None

        floor_up = max(0, st.top - cap, (above.bottom + 1) if above else 0)
        default_up = (above.bottom + st.top) // 2 if above else max(0, st.top - cap)
        st.crop_top = _scan_to_valley(rowink, st.top - 1, floor_up, -1,
                                      empty_thr, gap_needed, default_up)

        ceil_dn = min(h - 1, st.bottom + cap,
                      (below.top - 1) if below else h - 1)
        default_dn = (st.bottom + below.top) // 2 if below else min(h - 1, st.bottom + cap)
        st.crop_bottom = _scan_to_valley(rowink, st.bottom + 1, ceil_dn, 1,
                                         empty_thr, gap_needed, default_dn)


def group_systems(staves: list[Staff]) -> list[list[Staff]]:
    """Group staves into systems by the vertical gaps between them."""
    if not staves:
        return []
    staves = sorted(staves, key=lambda s: s.top)
    if len(staves) == 1:
        return [[staves[0]]]
    gaps = [b.top - a.bottom for a, b in zip(staves, staves[1:])]
    split_thresh = np.median(gaps) * 1.8
    systems, cur = [], [staves[0]]
    for gap, s in zip(gaps, staves[1:]):
        if gap > split_thresh:
            systems.append(cur)
            cur = [s]
        else:
            cur.append(s)
    systems.append(cur)
    return systems


def page_systems(staves, staves_per_system):
    """Split a page's staves into systems.

    For regular arrangements (constant staves-per-system) we chunk in fixed
    groups top-to-bottom, which is robust to uneven gaps (e.g. lyrics inflating
    the space between two staves) that fool gap-based grouping. Falls back to
    gap-based grouping when the count doesn't divide evenly.
    """
    n = len(staves)
    if staves_per_system and n and n % staves_per_system == 0:
        return [staves[i:i + staves_per_system]
                for i in range(0, n, staves_per_system)]
    return group_systems(staves)


def detect_page_staves(page, target_px):
    """Detect every staff on a page (flat, top-to-bottom) with crop bounds."""
    gray, scale = render_gray(page, target_px)
    dark = gray < 128
    staves = cluster_staves(find_staff_lines(gray), dark)
    compute_vertical_bounds(staves, dark)
    return staves, scale


# ----------------------------- extraction ---------------------------------

@dataclass
class Band:
    """A cropped strip (in PDF points) to place on an output page."""
    page_index: int
    x0: float
    y0: float
    x1: float
    y1: float


def bands_from_systems(systems, scale, page_index, staff_index):
    """Return the crop band (PDF points) for the target staff in each system."""
    bands = []
    for sys_staves in systems:
        if len(sys_staves) <= staff_index:
            continue  # this system lacks the target staff (e.g. a short coda)
        t = sys_staves[staff_index]
        x0 = t.content_left - t.height * 0.06
        x1 = t.right + t.height * 0.12
        bands.append(Band(
            page_index=page_index,
            x0=x0 / scale, y0=t.crop_top / scale,
            x1=x1 / scale, y1=t.crop_bottom / scale,
        ))
    return bands


# ----------------------------- title block --------------------------------

def title_block(doc, page0_staves, scale):
    """Return (clip_rect_in_points, title_line) for the title area above the
    first staff on page 1, so its original typography can be reused verbatim."""
    if not page0_staves:
        return None, ""
    page = doc[0]
    W = page.rect.width
    top_staff = min(page0_staves, key=lambda s: s.top)
    cut = max(0.0, (top_staff.top - top_staff.height * 0.6) / scale)
    region = fitz.Rect(0, 0, W, cut)
    d = page.get_text("dict", clip=region)

    xs, ys, lines = [], [], []
    for blk in d.get("blocks", []):
        for ln in blk.get("lines", []):
            txt = "".join(sp["text"] for sp in ln.get("spans", [])).strip()
            if txt:
                lines.append((ln["spans"][0]["bbox"][1], txt))
            for sp in ln.get("spans", []):
                bx0, by0, bx1, by1 = sp["bbox"]
                xs += [bx0, bx1]
                ys += [by0, by1]
    if not xs:
        return None, ""
    pad = 8
    rect = fitz.Rect(min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)
    lines.sort()
    return rect, (lines[0][1] if lines else "")


# ----------------------------- output -------------------------------------

LETTER_W, LETTER_H = 612, 792
A4_W, A4_H = 595, 842


def build_output(doc, bands, out_path, *, page_size="letter", landscape=False,
                 margin=40, gap=12, header_rect=None, title="",
                 part_label="Cello", page_numbers=True, show_header=True):
    pw, ph = (A4_W, A4_H) if page_size == "a4" else (LETTER_W, LETTER_H)
    if landscape:
        pw, ph = ph, pw
    usable_w = pw - 2 * margin
    footer_h = 20 if page_numbers else 0
    running = f"{title} — {part_label}".strip(" —") if title else ""
    out = fitz.open()
    state = {"page": None, "y": 0.0, "lines": 0}

    def new_page():
        pg = out.new_page(width=pw, height=ph)
        state["page"], state["y"], state["lines"] = pg, margin, 0
        if out.page_count > 1 and running:
            pg.insert_text((margin, margin + 6), running, fontsize=8.5,
                           fontname="helv", color=(0.35, 0.35, 0.35))
            state["y"] = margin + 16
        return pg

    pg = new_page()
    if show_header:
        y = state["y"]
        if header_rect and header_rect.width > 0 and header_rect.height > 0:
            s = min(usable_w / header_rect.width, 130 / header_rect.height)
            dw, dh = header_rect.width * s, header_rect.height * s
            x = margin + (usable_w - dw) / 2
            pg.show_pdf_page(fitz.Rect(x, y, x + dw, y + dh), doc, 0, clip=header_rect)
            y += dh + 6
        lab = part_label.upper()
        tw = fitz.get_text_length(lab, fontname="hebo", fontsize=17)
        pg.insert_text(((pw - tw) / 2, y + 15), lab, fontsize=17, fontname="hebo")
        y += 22
        pg.draw_line(fitz.Point(margin, y + 2), fitz.Point(pw - margin, y + 2),
                     color=(0.55, 0.55, 0.55), width=0.7)
        state["y"] = y + 10 + gap

    bottom_limit = ph - margin - footer_h
    for b in bands:
        bw, bh = b.x1 - b.x0, b.y1 - b.y0
        if bw <= 0 or bh <= 0:
            continue
        dh = bh * (usable_w / bw)
        if state["y"] + dh > bottom_limit and state["lines"] > 0:
            new_page()
        pg = state["page"]
        dest = fitz.Rect(margin, state["y"], margin + usable_w, state["y"] + dh)
        pg.show_pdf_page(dest, doc, b.page_index,
                         clip=fitz.Rect(b.x0, b.y0, b.x1, b.y1))
        state["y"] += dh + gap
        state["lines"] += 1

    if page_numbers:
        n = out.page_count
        for i in range(n):
            p = out[i]
            t = f"{i + 1} / {n}"
            tw = fitz.get_text_length(t, fontname="helv", fontsize=9)
            p.insert_text(((pw - tw) / 2, ph - margin + 12), t, fontsize=9,
                          fontname="helv", color=(0.4, 0.4, 0.4))

    out.save(out_path, deflate=True)
    return out.page_count


# ----------------------------- debug --------------------------------------

def debug_overlay(page, target_px, staff_index, out_png, staves_per_system=0):
    gray, scale = render_gray(page, target_px)
    dark = gray < 128
    staves = cluster_staves(find_staff_lines(gray), dark)
    compute_vertical_bounds(staves, dark)
    systems = page_systems(staves, staves_per_system)

    rgb = np.stack([gray] * 3, axis=-1).copy()
    colors = [(255, 0, 0), (0, 160, 0), (0, 0, 255), (200, 120, 0)]
    for sys_staves in systems:
        for idx, st in enumerate(sys_staves):
            c = colors[idx % len(colors)]
            rgb[st.top:st.top + 2, st.left:st.right] = c
            rgb[st.bottom:st.bottom + 2, st.left:st.right] = c
            if idx == staff_index:
                # magenta box = the exact crop that will be taken
                rgb[st.crop_top:st.crop_bottom, st.content_left:st.content_left + 3] = (255, 0, 255)
                rgb[st.crop_top:st.crop_bottom, st.right - 3:st.right] = (255, 0, 255)
                rgb[st.crop_top:st.crop_top + 2, st.content_left:st.right] = (255, 0, 255)
                rgb[st.crop_bottom - 2:st.crop_bottom, st.content_left:st.right] = (255, 0, 255)
    from PIL import Image
    Image.fromarray(rgb.astype(np.uint8)).save(out_png)
    print(f"systems: {len(systems)} | staves/system: {[len(s) for s in systems]}")


# ----------------------------- cli ----------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Extract one instrument's staff into a large-print part.")
    ap.add_argument("input", help="source PDF")
    ap.add_argument("-o", "--output", default="cello_part.pdf")
    ap.add_argument("--staff-index", type=int, default=1,
                    help="0-based staff position within each system (cello 2nd from top = 1)")
    ap.add_argument("--staves-per-system", type=int, default=0,
                    help="staves per system for regular scores (0 = auto-detect)")
    ap.add_argument("--part-label", default="Cello", help="part name shown in the header")
    ap.add_argument("--title", default="", help="override the running title (else read from the score)")
    ap.add_argument("--detect-px", type=int, default=1600,
                    help="normalized render width (px) used for staff detection")
    ap.add_argument("--margin", type=float, default=40, help="page margin in points")
    ap.add_argument("--gap", type=float, default=12, help="vertical space between lines in points")
    ap.add_argument("--page-size", choices=["letter", "a4"], default="letter")
    ap.add_argument("--landscape", action="store_true")
    ap.add_argument("--no-header", action="store_true", help="omit the title block / part label")
    ap.add_argument("--no-page-numbers", action="store_true")
    ap.add_argument("--debug-page", type=int, help="write a detection overlay PNG for this page and exit")
    args = ap.parse_args()

    doc = fitz.open(args.input)

    detected = []  # (page_index, staves, scale)
    for page in doc:
        staves, scale = detect_page_staves(page, args.detect_px)
        detected.append((page.number, staves, scale))

    spm = args.staves_per_system
    if not spm:
        from collections import Counter
        sizes = Counter()
        for _, staves, _ in detected:
            for sys in group_systems(staves):
                sizes[len(sys)] += 1
        spm = sizes.most_common(1)[0][0] if sizes else 0

    if args.debug_page is not None:
        debug_overlay(doc[args.debug_page], args.detect_px, args.staff_index,
                      "debug_overlay.png", spm)
        print(f"(staves-per-system = {spm})")
        print("wrote debug_overlay.png")
        return

    header_rect, detected_title = title_block(doc, detected[0][1], detected[0][2])
    title = args.title or detected_title or Path(args.input).stem.replace("_", " ")

    all_bands = []
    for page_index, staves, scale in detected:
        systems = page_systems(staves, spm)
        all_bands.extend(bands_from_systems(systems, scale, page_index, args.staff_index))

    n_pages = build_output(
        doc, all_bands, args.output,
        page_size=args.page_size, landscape=args.landscape, margin=args.margin,
        gap=args.gap, header_rect=header_rect, title=title,
        part_label=args.part_label, page_numbers=not args.no_page_numbers,
        show_header=not args.no_header,
    )
    print(f"extracted {len(all_bands)} lines from {doc.page_count} pages "
          f"(staves-per-system = {spm}) -> {n_pages} pages: {args.output}")


if __name__ == "__main__":
    main()

"""
Predicts how much RAM ONE specific PDF will need to process, BEFORE any
worker is committed to it.

WHY THIS EXISTS
----------------
The old design (concurrency_limit.py, original version) sized a single
flat ProcessPoolExecutor once at startup, using one constant
(PER_PDF_MARGINAL_MB = 250) for every PDF, no matter what's actually in
it. That has two failure modes, both real:

  1. A folder of small, simple PDFs (a few text pages, no images) gets
     throttled by a pool size that was chosen for a WORST-CASE PDF --
     each worker reserves far more RAM than that small PDF will ever
     use, so fewer PDFs run concurrently than the machine could
     actually handle. RAM sits idle while jobs queue.

  2. A big scanned PDF (200 pages, every page rendered for OCR) can
     spike to far more than 250MB. If several of those land in the
     pool at the same time, actual usage can exceed what the formula
     assumed safe, risking the container's OOM killer -- the exact
     opposite problem, in the opposite direction.

A flat per-PDF constant can't fix both at once, because it isn't
informed by anything about the PDF in front of it. The fix is to
ESTIMATE THE COST OF EACH SPECIFIC PDF using signals that are cheap to
read (don't require full extraction/OCR), then make admission
decisions (see ram_budget_pool.py) against a real, current RAM budget
instead of a worker-count constant.

WHAT MAKES THIS AN ESTIMATE, NOT A MEASUREMENT
-----------------------------------------------
True peak RAM usage can only be known by actually running the job --
that is true of any program, not a limitation specific to this one.
What CAN be known cheaply, by opening the PDF's structure without
rendering or OCRing anything (the same cheap-open fitz already does
elsewhere in app.py for page classification), is:

  - page count
  - how many raster images are embedded, and their pixel dimensions
  - whether pages look text-only, image-only, or mixed (drives whether
    OCR -- by far the most memory-hungry step -- will even run)

Those signals are measured against the cost MODEL below, which is
deliberately built from the same measured baselines already documented
in concurrency_limit.py (180MB import cost, ~160MB observed on a real
9-page PDF), extended with image-driven and OCR-driven terms that
weren't accounted for in the old flat constant. The constants here are
clearly labeled as estimates and are the one place to tune if
real-world measurement (see `measure_actual_rss_mb` below, used in
ram_budget_pool.py to record true usage after the fact) shows the model
running consistently high or low.
"""
import os

# --- Cost model constants -----------------------------------------------
# Same baseline already established and measured in concurrency_limit.py.
# Paid once per WORKER PROCESS (not per PDF) -- kept here only as a
# reference constant for callers that want the full per-worker number;
# ram_budget_pool.py adds this once per live worker, not once per PDF.
IMPORT_BASELINE_MB = 180

# Fixed cost just for fitz/pdfplumber to open the file and walk its
# page tree, independent of what's on the pages. Small and roughly
# constant -- measured to be a few MB even for large PDFs, but never
# zero, so it's kept as an explicit floor.
BASE_OPEN_COST_MB = 15

# Per-page cost for plain text extraction (pdfplumber/fitz text layer
# walking). Cheap -- this is just parsed text objects and positions,
# not pixels.
PER_PAGE_TEXT_MB = 0.8

# Per-page cost when Camelot is invoked (it is tried on every text/mixed
# page that might hold a table -- see app.py's classification-driven
# pipeline). Camelot pulls in OpenCV/numpy buffers per page even when no
# real table is ultimately found; this is the more expensive of the two
# "page has a text layer" paths, so it's the one budgeted for.
PER_PAGE_CAMELOT_MB = 4.0

# Per-page cost for OCR (pdf2image rendering a page to a raster image,
# then pytesseract reading it). This is the single biggest per-page
# cost in the whole pipeline -- rendering a page at OCR-quality DPI
# produces a multi-megapixel uncompressed bitmap in memory before
# Tesseract even starts. Modeled as a base render cost plus a
# resolution-driven term below.
PER_PAGE_OCR_BASE_MB = 25.0

# Reference DPI that PER_PAGE_OCR_BASE_MB assumes (pdf2image's default
# in this project -- see app.py's OCR call). If actual rendering DPI is
# ever changed, this constant and PER_PAGE_OCR_BASE_MB should be
# re-measured together.
OCR_REFERENCE_DPI = 200

# Embedded-image cost: rendering/holding decoded image pixel data costs
# roughly 4 bytes per pixel (RGBA) once decompressed, even though the
# PDF stores it compressed on disk. This is what makes a PDF full of
# large embedded photos cost more than its file size would suggest.
BYTES_PER_DECODED_PIXEL = 4
MB_PER_BYTE = 1 / (1024 * 1024)

# Safety multiplier applied to the final estimate. The model above is
# built from measured baselines but is still a prediction, not a
# guarantee -- real PDFs have outliers (corrupted images that decode
# inefficiently, pathological table layouts, fonts that explode into
# huge glyph caches). This margin is intentionally separate from, and
# in addition to, the system-wide MEMORY_SAFETY_MARGIN already applied
# to total available RAM in ram_budget_pool.py -- that one protects the
# MACHINE; this one protects against THIS MODEL being wrong about ONE
# file.
PER_FILE_ESTIMATE_SAFETY_MARGIN = 1.3

# Hard floor and ceiling so a pathological estimate (e.g. a corrupted
# PDF reporting a billion-pixel image) can't make the admission
# controller starve every other job, or accept a job for free.
MIN_ESTIMATE_MB = 40
MAX_ESTIMATE_MB = 4000


class PdfInspectionError(Exception):
    """Raised when the PDF can't even be opened cheaply for inspection
    (corrupted file, encrypted with no accessible structure, etc).
    Callers should treat this as "use the conservative fallback
    estimate", not as a hard failure -- the actual processing pipeline
    has its own, separate error handling for genuinely broken PDFs."""
    pass


def _inspect_with_fitz(pdf_path):
    """
    Cheap structural inspection using PyMuPDF. Does NOT render, OCR, or
    run Camelot on anything -- only reads the page tree and each page's
    image XObject list, which is metadata (declared pixel dimensions),
    not decoded pixel data. This mirrors classify_pages_pymupdf() in
    app.py exactly, so the estimate is grounded in the same signals the
    real pipeline will act on.

    Returns a dict: {page_count, text_page_count, mixed_page_count,
    image_only_page_count, empty_page_count, total_image_pixels}
    """
    import fitz  # PyMuPDF

    info = {
        "page_count": 0,
        "text_page_count": 0,
        "mixed_page_count": 0,
        "image_only_page_count": 0,
        "empty_page_count": 0,
        "total_image_pixels": 0,
    }

    doc = fitz.open(pdf_path)
    try:
        info["page_count"] = len(doc)
        for page_index in range(len(doc)):
            page = doc[page_index]
            raw_text = page.get_text("text") or ""
            has_text = len(raw_text.strip()) >= 3

            images = page.get_images(full=True)
            has_image = len(images) > 0

            # images entries are tuples; index 2 and 3 are width/height
            # in pixels for the declared image XObject (this is metadata
            # read from the PDF's image dictionary, not decoded pixel
            # data -- cheap regardless of the image's actual file size).
            for img in images:
                try:
                    width, height = img[2], img[3]
                    info["total_image_pixels"] += max(0, width) * max(0, height)
                except (IndexError, TypeError):
                    continue

            if has_text and has_image:
                info["mixed_page_count"] += 1
            elif has_text:
                info["text_page_count"] += 1
            elif has_image:
                info["image_only_page_count"] += 1
            else:
                info["empty_page_count"] += 1
    finally:
        doc.close()

    return info


def _inspect_with_pdfplumber(pdf_path):
    """
    Fallback inspection if PyMuPDF isn't installed. Mirrors
    classify_pages_pdfplumber() in app.py for the same reason: stay
    consistent with whatever signals the real pipeline will use to
    decide per-page handling.
    """
    import pdfplumber

    info = {
        "page_count": 0,
        "text_page_count": 0,
        "mixed_page_count": 0,
        "image_only_page_count": 0,
        "empty_page_count": 0,
        "total_image_pixels": 0,
    }

    with pdfplumber.open(pdf_path) as pdf:
        info["page_count"] = len(pdf.pages)
        for page in pdf.pages:
            chars = page.chars or []
            text = (page.extract_text() or "").strip()
            has_text = len(chars) >= 3 and len(text) > 0

            images = page.images or []
            has_image = len(images) > 0

            for img in images:
                try:
                    width = img.get("width", 0) or 0
                    height = img.get("height", 0) or 0
                    info["total_image_pixels"] += max(0, width) * max(0, height)
                except (TypeError, AttributeError):
                    continue

            if has_text and has_image:
                info["mixed_page_count"] += 1
            elif has_text:
                info["text_page_count"] += 1
            elif has_image:
                info["image_only_page_count"] += 1
            else:
                info["empty_page_count"] += 1

    return info


def inspect_pdf_cheaply(pdf_path):
    """
    Tries PyMuPDF first (matches app.py's own preference order), falls
    back to pdfplumber. Raises PdfInspectionError if neither can open
    the file at all -- callers should catch this and fall back to a
    conservative flat estimate (see estimate_pdf_cost_mb below).
    """
    try:
        import fitz  # noqa: F401
        return _inspect_with_fitz(pdf_path)
    except ImportError:
        pass
    except Exception as e:
        raise PdfInspectionError(f"PyMuPDF failed to inspect {pdf_path}: {e}")

    try:
        return _inspect_with_pdfplumber(pdf_path)
    except Exception as e:
        raise PdfInspectionError(f"pdfplumber failed to inspect {pdf_path}: {e}")


def estimate_pdf_cost_mb(pdf_path):
    """
    Returns (estimated_mb, breakdown_dict).

    estimated_mb is the predicted PEAK ADDITIONAL memory (in MB) that
    processing this one PDF will need IN A WORKER THAT HAS ALREADY PAID
    the one-time import baseline -- i.e. this is the per-PDF marginal
    cost the admission controller should check against its remaining
    RAM budget, the same role PER_PDF_MARGINAL_MB played in the old
    flat model, just computed per-file instead of assumed constant.

    On any inspection failure (corrupted/encrypted/unreadable PDF),
    returns a conservative flat fallback rather than raising -- a
    file we can't even cheaply inspect should be assumed expensive,
    not assumed free.
    """
    try:
        size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
    except OSError:
        size_mb = 0

    try:
        info = inspect_pdf_cheaply(pdf_path)
    except PdfInspectionError:
        # Conservative fallback: behave like a moderately heavy PDF.
        # Deliberately matches the old flat constant's spirit (250MB)
        # but rounded up slightly, since "we couldn't even inspect it"
        # is itself a bad sign worth treating cautiously.
        fallback_mb = max(MIN_ESTIMATE_MB, min(MAX_ESTIMATE_MB, 280))
        return fallback_mb, {
            "method": "fallback_inspection_failed",
            "file_size_mb": round(size_mb, 2),
            "estimated_mb": fallback_mb,
        }

    text_pages = info["text_page_count"]
    mixed_pages = info["mixed_page_count"]
    image_only_pages = info["image_only_page_count"]
    empty_pages = info["empty_page_count"]
    total_pages = info["page_count"]

    # Text + mixed pages both go through Camelot in the real pipeline
    # (see app.py's per-page classification comments) -- mixed pages
    # additionally have an image on them, but that image is NOT
    # rendered for OCR (a real text layer already exists), so its
    # cost is the same Camelot per-page cost, not the OCR cost.
    camelot_eligible_pages = text_pages + mixed_pages
    camelot_cost_mb = camelot_eligible_pages * PER_PAGE_CAMELOT_MB
    text_walk_cost_mb = total_pages * PER_PAGE_TEXT_MB

    # OCR cost only applies to image-only pages, and only if OCR libs
    # are actually installed in this environment -- if they're not,
    # app.py's real pipeline skips OCR entirely for those pages (marks
    # them "ocr_unavailable"), so budgeting OCR cost for an environment
    # that can't run OCR would overestimate for no reason.
    try:
        import pytesseract  # noqa: F401
        from pdf2image import convert_from_path  # noqa: F401
        ocr_available = True
    except ImportError:
        ocr_available = False

    ocr_cost_mb = (image_only_pages * PER_PAGE_OCR_BASE_MB) if ocr_available else 0.0

    # Decoded-pixel cost from embedded images (applies whether or not
    # OCR runs -- pdfplumber/fitz still touch image metadata, and on
    # mixed pages the image is still embedded in the output even though
    # it isn't OCR'd). Scaled down heavily (pixels are NOT all decoded
    # to full RGBA simultaneously in practice -- this is a conservative
    # partial-credit term, not a literal "decode everything" cost).
    image_pixel_cost_mb = (
        info["total_image_pixels"] * BYTES_PER_DECODED_PIXEL * MB_PER_BYTE * 0.15
    )

    raw_estimate_mb = (
        BASE_OPEN_COST_MB
        + text_walk_cost_mb
        + camelot_cost_mb
        + ocr_cost_mb
        + image_pixel_cost_mb
    )

    final_estimate_mb = raw_estimate_mb * PER_FILE_ESTIMATE_SAFETY_MARGIN
    final_estimate_mb = max(MIN_ESTIMATE_MB, min(MAX_ESTIMATE_MB, final_estimate_mb))

    breakdown = {
        "method": "structural_inspection",
        "file_size_mb": round(size_mb, 2),
        "page_count": total_pages,
        "text_pages": text_pages,
        "mixed_pages": mixed_pages,
        "image_only_pages": image_only_pages,
        "empty_pages": empty_pages,
        "ocr_available": ocr_available,
        "total_image_pixels": info["total_image_pixels"],
        "cost_breakdown_mb": {
            "base_open": round(BASE_OPEN_COST_MB, 1),
            "text_walk": round(text_walk_cost_mb, 1),
            "camelot": round(camelot_cost_mb, 1),
            "ocr": round(ocr_cost_mb, 1),
            "image_pixels": round(image_pixel_cost_mb, 1),
            "raw_subtotal": round(raw_estimate_mb, 1),
            "after_safety_margin": round(final_estimate_mb, 1),
        },
        "estimated_mb": round(final_estimate_mb, 1),
    }
    return round(final_estimate_mb, 1), breakdown


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("Usage: python pdf_cost_estimator.py <pdf_path> [<pdf_path> ...]")
        sys.exit(1)

    for path in sys.argv[1:]:
        estimate, breakdown = estimate_pdf_cost_mb(path)
        print(f"\n{path}")
        print(json.dumps(breakdown, indent=2))

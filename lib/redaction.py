"""Search and redact PDFs and images, including scanned pages handled by OCR."""

from pathlib import Path

from . import pii, tesseract
from .logging_setup import logger

# ─── Redaction logic ───────────────────────────────────────────────────────────

import pymupdf as fitz  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

SUPPORTED_EXTS = {'.pdf', '.jpg', '.jpeg', '.png', '.tif', '.tiff', '.bmp'}

OCR_DPI = 300       # resolution scanned PDF pages are rendered at for OCR
OCR_TIMEOUT = 300   # seconds before Tesseract is abandoned on one page or image

class RedactionError(Exception):
    """A file that cannot be redacted. Reported as an error, never as a skip."""

# Shown to the user in the results list; technical details go to the server log
MSG_PROTECTED = 'This PDF is password-protected. Remove the password and try again.'
MSG_UNREADABLE_PDF = "This file couldn't be opened. It may be damaged or not a real PDF."
MSG_UNREADABLE_IMAGE = "This image couldn't be opened. It may be damaged."
MSG_NEEDS_TESSERACT = 'Reading text in scanned pages and images needs Tesseract. Install it with: brew install tesseract'
MSG_OCR_TIMEOUT = 'Reading the text in this file took too long, so it was stopped.'
MSG_CRASHED = "This file couldn't be processed. It may be damaged."
MSG_UNEXPECTED = 'Something went wrong with this file. Details are in the server log.'

def _ocr_matches(img, terms: list[str], cancel_event, pii_kinds=()):
    """OCR a PIL image and find every term and PII occurrence.

    Words are joined per OCR line, so multi-word terms ("Alice Smith") match
    across word boxes. Returns (matches, pii_counts): each match is
    (label, [(x, y, w, h), ...]) where label is ('term', term_no) or
    ('pii', kind), and pii_counts maps kind to the number of detected values.
    Returns None if cancelled.
    """
    import pytesseract
    tesseract_path = tesseract._find_tesseract()
    if tesseract_path:
        pytesseract.pytesseract.tesseract_cmd = tesseract_path
    try:
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT, timeout=OCR_TIMEOUT)
    except pytesseract.TesseractNotFoundError as e:
        logger.error('Tesseract not found: %s', e)
        raise RedactionError(MSG_NEEDS_TESSERACT) from e
    except RuntimeError as e:
        if 'timeout' in str(e).lower():
            logger.warning('OCR gave up after %ds', OCR_TIMEOUT)
            raise RedactionError(MSG_OCR_TIMEOUT) from e
        raise
    n = len(data['text'])
    lines = {}
    for i in range(n):
        if data['text'][i].strip():
            key = tuple(data.get(k, [0] * n)[i] for k in ('block_num', 'par_num', 'line_num'))
            lines.setdefault(key, []).append(i)
    found = []
    pii_counts = {}
    for word_indexes in lines.values():
        if cancel_event.is_set():
            return None
        line, spans = '', []
        for i in word_indexes:
            if line:
                line += ' '
            start = len(line)
            line += data['text'][i].strip()
            spans.append((start, len(line), i))
        lower = line.lower()
        covered = set()
        for term_no, term in enumerate(terms, 1):
            needle = term.lower()
            pos = lower.find(needle)
            while pos != -1:
                stop = pos + len(needle)
                boxes = [(data['left'][i], data['top'][i], data['width'][i], data['height'][i])
                         for s, e, i in spans if s < stop and e > pos]
                boxes = [b for b in boxes if b[2] > 0 and b[3] > 0]
                if boxes:
                    found.append((('term', term_no), boxes))
                    covered.add((pos, stop))
                pos = lower.find(needle, stop)
        for match in pii.find_pii(line, pii_kinds):
            if (match.start, match.end) in covered:
                continue  # the user already listed this exact value
            boxes = [(data['left'][i], data['top'][i], data['width'][i], data['height'][i])
                     for s, e, i in spans if s < match.end and e > match.start]
            boxes = [b for b in boxes if b[2] > 0 and b[3] > 0]
            if boxes:
                found.append((('pii', match.kind), boxes))
                pii_counts[match.kind] = pii_counts.get(match.kind, 0) + 1
    return found, pii_counts

def _hidden_matches(doc, terms: list[str], pii_kinds=()):
    """Count terms and PII found in properties, XMP metadata and bookmarks.

    Returns (term_matches, pii_counts). Metadata is cleared from every saved
    copy anyway; detecting PII here also triggers a cleaned copy when that is
    the only place it appears.
    """
    hidden = [v for v in doc.metadata.values() if v]
    hidden.append(doc.get_xml_metadata() or '')
    hidden.extend(entry[1] for entry in doc.get_toc())
    matches = sum(h.lower().count(term.lower()) for h in hidden for term in terms)
    pii_counts = {}
    for text in hidden:
        for match in pii.find_pii(text, pii_kinds):
            pii_counts[match.kind] = pii_counts.get(match.kind, 0) + 1
    return matches, pii_counts

def _open_pdf(path):
    doc = fitz.open(str(path))
    if doc.needs_pass:
        doc.close()
        raise RedactionError(MSG_PROTECTED)
    return doc

def _analyze_pages(doc, page_numbers, terms: list[str], cancel_event, label: str, pii_kinds=()):
    """Find everything to redact on the given pages, without changing the document.

    Pages without a text layer but with images are scanned pages and are OCR'd.
    Returns ({page_number: [(x0, y0, x1, y1), ...]}, matches, ocr_pages,
    pii_counts) with rects in page coordinates, or None if cancelled.
    """
    rects_by_page, matches, ocr_pages = {}, 0, 0
    pii_counts = {}
    for pno in page_numbers:
        if cancel_event.is_set():
            return None
        page = doc[pno]
        rects = []
        textpage = page.get_textpage()  # extract the text once, search it for every term
        page_text = textpage.extractText()
        # Detected values join the term list as literal needles, so the existing
        # search/redaction path applies to them unchanged.
        pii_needles = {}
        for match in pii.find_pii(page_text, pii_kinds):
            raw = page_text[match.start:match.end]
            pii_needles.setdefault(raw, set()).add(match.kind)
        needles = list(terms)
        seen = set(terms)
        for raw in pii_needles:
            if raw not in seen:
                needles.append(raw)
                seen.add(raw)
        for term_no, term in enumerate(needles, 1):
            found = page.search_for(term, textpage=textpage)
            if not found:
                continue
            if term_no <= len(terms):
                # Terms are sensitive: log the term's position in the list, never its text
                logger.debug('%s page %d: term #%d found %d time(s)', label, pno + 1, term_no, len(found))
            for kind in pii_needles.get(term, ()):
                logger.debug('%s page %d: detected %s (%d occurrence(s))',
                             label, pno + 1, pii.LABELS[kind], len(found))
                pii_counts[kind] = pii_counts.get(kind, 0) + len(found)
            rects.extend(tuple(r) for r in found)
            matches += len(found)
        if not page_text.strip() and page.get_images():
            try:
                import pytesseract  # noqa: F401
            except ImportError:
                logger.error('%s page %d is scanned and needs OCR, but pytesseract is not installed', label, pno + 1)
                raise RedactionError(MSG_NEEDS_TESSERACT)
            ocr_pages += 1
            logger.debug('%s page %d has no text layer; running OCR', label, pno + 1)
            pix = page.get_pixmap(dpi=OCR_DPI)
            ocr_result = _ocr_matches(Image.frombytes('RGB', (pix.width, pix.height), pix.samples),
                                      terms, cancel_event, pii_kinds)
            if ocr_result is None:
                return None
            found, page_pii = ocr_result
            # Pixmap pixels -> page points, then undo any page rotation
            to_page = fitz.Matrix(72 / OCR_DPI, 72 / OCR_DPI) * page.derotation_matrix
            for match_label, boxes in found:
                if match_label[0] == 'term':
                    logger.debug('%s page %d: term #%d matched OCR text (%d word box(es))',
                                 label, pno + 1, match_label[1], len(boxes))
                else:
                    logger.debug('%s page %d: %s matched OCR text (%d word box(es))',
                                 label, pno + 1, pii.LABELS[match_label[1]], len(boxes))
                rects.extend(tuple(fitz.Rect(x, y, x + w, y + h) * to_page) for x, y, w, h in boxes)
            matches += len(found)
            for kind, count in page_pii.items():
                pii_counts[kind] = pii_counts.get(kind, 0) + count
        if rects:
            rects_by_page[pno] = rects
    return rects_by_page, matches, ocr_pages, pii_counts

def _write_redacted_pdf(doc, rects_by_page, terms: list[str], output_path: Path, label: str, matches: int,
                        ocr_pages: int, pii_counts=None, pii_kinds=()):
    """Apply the redactions and save. If nothing matched anywhere, write nothing."""
    for pno, rects in rects_by_page.items():
        page = doc[pno]
        for r in rects:
            page.add_redact_annot(fitz.Rect(r), fill=(0, 0, 0))
        # Remove the text and blank the image pixels under each box, not just cover them
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)
    metadata_matches, metadata_pii = _hidden_matches(doc, terms, pii_kinds)
    counts = dict(pii_counts or {})
    for kind, count in metadata_pii.items():
        counts[kind] = counts.get(kind, 0) + count
    result = {'matches': matches, 'pages': len(doc)}
    if ocr_pages:
        result['ocr_pages'] = ocr_pages
    if metadata_matches:
        result['metadata_matches'] = metadata_matches
    if counts:
        result['pii'] = counts
    if matches == 0 and metadata_matches == 0 and not counts:
        return {**result, 'skipped': True}
    # Properties, XMP metadata and bookmarks can repeat sensitive text: clear them
    bookmarks = len(doc.get_toc())
    doc.set_metadata({})
    doc.del_xml_metadata()
    doc.set_toc([])
    logger.debug('%s: cleared document metadata and %d bookmark(s)', label, bookmarks)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # garbage=3 drops objects no longer referenced (old images, metadata) so
    # redacted content cannot be recovered from the file
    doc.save(str(output_path), garbage=3, deflate=True)
    return result

def _redact_pdf(input_path: Path, terms: list[str], output_path: Path, cancel_event, pii_kinds=()):
    """Redact matches into output_path. If nothing matches, write nothing."""
    logger.debug('Searching PDF %s for %d term(s) and %d PII type(s)', input_path, len(terms), len(pii_kinds))
    try:
        doc = _open_pdf(input_path)
    except RedactionError as e:
        return None, str(e)
    try:
        analysis = _analyze_pages(doc, range(len(doc)), terms, cancel_event, input_path.name, pii_kinds)
        if analysis is None:
            logger.info('Cancelled while searching %s', input_path)
            return None, 'cancelled'
        rects_by_page, matches, ocr_pages, pii_counts = analysis
        return _write_redacted_pdf(doc, rects_by_page, terms, output_path, input_path.name, matches,
                                   ocr_pages, pii_counts, pii_kinds), None
    except RedactionError as e:
        return None, str(e)
    finally:
        doc.close()

def _redact_image(input_path: Path, terms: list[str], output_path: Path, cancel_event, pii_kinds=()):
    """Redact OCR matches into output_path. If nothing matches, write nothing."""
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        logger.error('Cannot redact %s: pytesseract not installed', input_path)
        return None, MSG_NEEDS_TESSERACT
    logger.debug('Running OCR on image %s', input_path)
    try:
        img = Image.open(str(input_path))
        # Convert to RGB for drawing
        if img.mode != 'RGB':
            img = img.convert('RGB')
    except (OSError, Image.DecompressionBombError) as e:
        logger.warning('Cannot open image %s: %s', input_path, e)
        return None, MSG_UNREADABLE_IMAGE
    try:
        ocr_result = _ocr_matches(img, terms, cancel_event, pii_kinds)
    except RedactionError as e:
        return None, str(e)
    if ocr_result is None:
        logger.info('Cancelled while searching %s', input_path)
        return None, 'cancelled'
    found, pii_counts = ocr_result
    draw = ImageDraw.Draw(img)
    for match_label, boxes in found:
        for x, y, w, h in boxes:
            if match_label[0] == 'term':
                logger.debug('%s: term #%d matched OCR text at (%d, %d, %d, %d)',
                             input_path.name, match_label[1], x, y, w, h)
            else:
                logger.debug('%s: %s matched OCR text at (%d, %d, %d, %d)',
                             input_path.name, pii.LABELS[match_label[1]], x, y, w, h)
            draw.rectangle([x, y, x + w, y + h], fill=(0, 0, 0))
    if not found:
        return {'matches': 0, 'skipped': True}, None
    result = {'matches': len(found)}
    if pii_counts:
        result['pii'] = pii_counts
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(output_path))
    return result, None

def _remove_previous_output(output_path: Path) -> bool:
    """Drop output from a previous run so the output dir always reflects the
    current terms (do not invent a new name)."""
    if output_path.exists():
        output_path.unlink()
        return True
    return False

def _describe_pii_counts(counts):
    """Counts and type names only: detected values never reach the log."""
    return ', '.join(f"{n} {pii.LABELS[kind]}{'' if n == 1 else 's'}" for kind, n in counts.items())


def _file_info(input_path, output_path, result, err, had_previous_output):
    """Log a file's outcome and build its entry for the results list."""
    skipped = bool(result and result.get('skipped'))
    if err:
        logger.warning('Not redacted %s: %s', input_path, err)
    elif skipped:
        logger.info('Skipped %s: nothing matched; file left untouched, no output written%s',
                    input_path, ' (removed stale output from a previous run)' if had_previous_output else '')
    else:
        extra = ''
        if result.get('metadata_matches'):
            extra += f", {result['metadata_matches']} in metadata/bookmarks"
        if result.get('ocr_pages'):
            extra += f", {result['ocr_pages']} scanned page(s) OCR'd"
        if result.get('pii'):
            extra += f", {_describe_pii_counts(result['pii'])}"
        logger.info('Redacted %s -> %s (%d match(es)%s)', input_path, output_path, result['matches'], extra)
    info = {
        'path': None if (err or skipped) else str(output_path),
        'input': str(input_path),
        'result': result,
        'error': err,
    }
    if err == MSG_NEEDS_TESSERACT:
        info['needs_tesseract'] = True  # lets the UI explain the cause in its summary
    return info

def _redact_file(input_path: Path, output_path: Path, terms: list[str], cancel_event, pii_kinds=()):
    logger.info('Redacting %s', input_path)
    had_previous_output = _remove_previous_output(output_path)
    suffix = input_path.suffix.lower()
    if suffix == '.pdf':
        result, err = _redact_pdf(input_path, terms, output_path, cancel_event, pii_kinds)
    elif suffix in {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.bmp'}:
        result, err = _redact_image(input_path, terms, output_path, cancel_event, pii_kinds)
    else:
        result, err = None, "This file type isn't supported."
    return _file_info(input_path, output_path, result, err, had_previous_output)


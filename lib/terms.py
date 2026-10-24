"""Turn the textarea or terms-file text into a clean term list."""

from .logging_setup import logger

def _parse_terms(raw_text: str) -> list[str]:
    terms = []
    seen = set()
    for line in raw_text.splitlines():
        for part in line.split(','):
            term = part.strip()
            if term and term not in seen:
                seen.add(term)
                terms.append(term)
    logger.debug('Parsed %d term(s) from %d characters of text', len(terms), len(raw_text))
    return terms

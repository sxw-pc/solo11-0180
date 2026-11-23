"""Offline OCR evidence for paper meal vouchers.

An OCR confidence orders the candidate vouchers read from one image.  It is
never proof that a voucher was issued to that ticket: issuance proof lives in
the issued book and in the globally unique assignment solved by
:mod:`mealassist.voucher`.
"""
import hashlib
import json

MAX_CONFIDENCE = 1.0


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def image_hash(image):
    """Hash the retained image bytes/text without trusting any caller label."""
    if isinstance(image, bytes):
        payload = b'bytes:' + image
    elif isinstance(image, str):
        payload = b'text:' + image.encode('utf-8')
    else:
        payload = b'json:' + canonical(image).encode('utf-8')
    return 'sha256:' + hashlib.sha256(payload).hexdigest()


def normalize_chars(chars):
    """Keep the raw OCR characters that a human reviewer needs to see."""
    return ''.join(ch for ch in str(chars) if not ch.isspace())


def ordered(candidates):
    """Return candidates in OCR preference order (score, then voucher id).

    The rank is stable and deterministic; it only decides which candidate the
    matcher tries first, never which candidates are considered valid.
    """
    indexed = list(enumerate(candidates))
    indexed.sort(key=lambda pair: (-float(pair[1].get('confidence', 0.0)),
                                   str(pair[1].get('voucher_id', '')),
                                   pair[0]))
    return [item for _, item in indexed]


def choose(candidates):
    """Legacy single-image pick used by the seed baseline.

    The result is a provisional OCR choice only.  It must not be turned into a
    merchant payable without a confirmed batch allocation.
    """
    if not candidates:
        raise ValueError('no_ocr_candidate')
    return max(candidates, key=lambda x: (x['confidence'], x['voucher_id']))


def validate_candidate(candidate, voucher):
    """Validate one OCR candidate against the issued book.

    ``voucher`` is the issued-book row for ``candidate['voucher_id']`` or
    ``None``.  Returns ``(ok, reason)``.
    """
    if not candidate.get('voucher_id'):
        return False, 'missing-voucher-id'
    try:
        amount = int(candidate.get('amount'))
    except (TypeError, ValueError):
        return False, 'invalid-candidate-amount'
    if amount <= 0:
        return False, 'invalid-candidate-amount'
    try:
        confidence = float(candidate.get('confidence', 0.0))
    except (TypeError, ValueError):
        return False, 'invalid-confidence'
    if not 0.0 <= confidence <= MAX_CONFIDENCE:
        return False, 'invalid-confidence'
    if not normalize_chars(candidate.get('chars', '')):
        return False, 'missing-ocr-characters'
    if voucher is None:
        return False, 'voucher-not-in-issued-book'
    if int(voucher['amount']) != amount:
        return False, 'candidate-amount-differs-from-issued-book'
    return True, 'candidate-valid'

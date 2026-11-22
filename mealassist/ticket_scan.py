def choose(candidates):
    if not candidates:
        raise ValueError('no_ocr_candidate')
    return max(candidates, key=lambda x: (x['confidence'], x['voucher_id']))

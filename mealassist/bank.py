"""A local mock bank with an external file lifetime.

The bank is the authority on whether money actually moved.  Its interface is
keyed by a *stable idempotency key* (the payable id), never by a caller chosen
payment id:

* ``transfer``  - submit a payment.  Re-submitting the same key returns the
  original receipt unchanged; it never moves money twice.  A same-key
  submission with a different payee/amount is kept as a ``conflict`` receipt
  and the original receipt is preserved.
* ``query`` / ``query_status`` - read back the final result for a key.
* ``reverse`` / ``refund`` - append a compensating transaction.  Bank facts are
  never deleted or rewritten.

Statuses: ``transferred`` (final success), ``accepted`` (bank took it, final
result still pending), ``rejected`` (bank refused), ``unknown`` (result
unknown), ``conflict`` (a second submission disagreed with the first).
"""
import fcntl, json, os, tempfile
from pathlib import Path

FINAL_STATUSES = ('transferred', 'rejected', 'reversed')
KNOWN_STATUSES = ('accepted', 'transferred', 'rejected', 'unknown', 'conflict', 'reversed')

def _read(path):
    if not path.exists():
        return []
    raw = path.read_text(encoding='utf-8').strip()
    if not raw:
        return []
    rows = json.loads(raw)
    if not isinstance(rows, list):
        raise ValueError('bank-file-corrupt')
    return rows

def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(rows, fh, ensure_ascii=False, separators=(',', ':'))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise

class _Lock:
    """Cross-process advisory lock around the bank file operations."""
    def __init__(self, path):
        self.path = Path(str(path) + '.lock')
        self.fh = None
    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, 'a+')
        fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        return self
    def __exit__(self, *exc):
        try: fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally: self.fh.close()

def _key(request):
    key = request.get('idempotency_key') or request.get('payment_id')
    if not key:
        raise ValueError('idempotency-key-required')
    return str(key)

def _find(rows, key, kind='payment'):
    for row in reversed(rows):
        if row.get('kind', 'payment') == kind and row.get('idempotency_key', row.get('payment_id')) == key:
            return row
    return None

def _receipt_id(rows, key, request, kind='payment'):
    base = f"{kind}:{key}:{request.get('payment_id') or key}"
    attempts = sum(1 for r in rows if r.get('idempotency_key', r.get('payment_id')) == key
                   and r.get('kind', 'payment') == kind)
    return base if attempts == 0 else f"{base}#{attempts + 1}"

def _receipt(key, request, status, kind='payment', conflict=None, reversal_of=None, receipt_id=None):
    row = {
        'receipt_id': receipt_id or f"{kind}:{key}:{request.get('payment_id') or key}",
        'kind': kind,
        'idempotency_key': key,
        'payment_id': request.get('payment_id') or key,
        'payable_id': request.get('payable_id'),
        'merchant': request.get('merchant'),
        'payee': request.get('payee') or request.get('merchant'),
        'amount': int(request.get('amount', 0)),
        'status': status,
    }
    if conflict: row['conflict'] = conflict
    if reversal_of: row['reversal_of'] = reversal_of
    return row

def transfer(file, request, status='transferred', conflict=None):
    """Submit a payment keyed by its stable idempotency key.

    ``status`` lets an offline test or fixture model the bank's answer
    (``transferred``/``accepted``/``rejected``/``unknown``).  It has no effect
    when the key was already submitted: the stored receipt wins.
    """
    if not isinstance(request, dict):
        raise ValueError('bank-request-invalid')
    if status not in KNOWN_STATUSES:
        raise ValueError('bank-status-invalid')
    path = Path(file)
    key = _key(request)
    with _Lock(path):
        rows = _read(path)
        prior = _find(rows, key)
        if prior is not None:
            same = (prior.get('payee') == (request.get('payee') or request.get('merchant'))
                    and int(prior.get('amount', 0)) == int(request.get('amount', 0)))
            if same:
                return dict(prior, deduplicated=True)
            # Keep the original receipt untouched; record the disagreement.
            disputed = _receipt(key, request, 'conflict',
                                conflict={'requested_payee': request.get('payee') or request.get('merchant'),
                                          'requested_amount': int(request.get('amount', 0)),
                                          'accepted_receipt': prior.get('receipt_id')},
                                receipt_id=_receipt_id(rows, key, request))
            rows.append(disputed)
            _write(path, rows)
            return dict(disputed)
        if conflict is not None:
            row = _receipt(key, request, 'conflict', conflict=conflict,
                           receipt_id=_receipt_id(rows, key, request))
        else:
            row = _receipt(key, request, status, receipt_id=_receipt_id(rows, key, request))
        rows.append(row)
        _write(path, rows)
        return dict(row)

def accept(file, request):
    return transfer(file, request, status='accepted')

def _latest(rows, key):
    found = None
    for row in rows:
        if row.get('kind', 'payment') == 'payment' and row.get('idempotency_key', row.get('payment_id')) == key:
            found = row
    return found

def query(file, key):
    path = Path(file)
    with _Lock(path):
        rows = _read(path)
    return dict(_latest(rows, str(key))) if _latest(rows, str(key)) else None

def query_status(file, key):
    row = query(file, key)
    return row['status'] if row else 'unknown-record'

def set_status(file, key, status):
    """Test/fixture hook: model the bank completing a pending receipt."""
    if status not in KNOWN_STATUSES:
        raise ValueError('bank-status-invalid')
    path = Path(file)
    with _Lock(path):
        rows = _read(path)
        row = _latest(rows, str(key))
        if row is None:
            raise KeyError('bank-receipt-not-found')
        row['status'] = status
        _write(path, rows)
        return dict(row)

def reverse(file, request):
    """Append a compensating transaction; the original receipt stays."""
    if not isinstance(request, dict):
        raise ValueError('bank-request-invalid')
    path = Path(file)
    key = _key(request)
    reversal_key = f"{key}:reversal"
    with _Lock(path):
        rows = _read(path)
        prior = _find(rows, reversal_key, kind='reversal')
        if prior is not None:
            return dict(prior, deduplicated=True)
        original = _find(rows, key)
        if original is None:
            raise KeyError('bank-receipt-not-found')
        if original.get('status') not in ('transferred',):
            raise ValueError('bank-receipt-not-transferred')
        # A conflicting submission must not reuse the original receipt id: the
        # unique receipt index would otherwise hide it, and the original must
        # stay exactly as recorded.
        row = _receipt(reversal_key, request, 'transferred', kind='reversal',
                       reversal_of=original.get('receipt_id'), receipt_id=_receipt_id(rows, reversal_key, request, 'reversal'))
        row['amount'] = -abs(int(request.get('amount', original.get('amount', 0))))
        rows.append(row)
        _write(path, rows)
        return dict(row)

def rows(file):
    path = Path(file)
    if not path.exists():
        return []
    with _Lock(path):
        return _read(path)

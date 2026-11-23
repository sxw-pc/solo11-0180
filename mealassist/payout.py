"""Merchant payout orchestration with bank-first recovery.

The invariant is simple: **the bank is asked about a payable before it is asked
to move money, and a payable never moves money twice**.  The stable business
key of a payout is the payable id; a caller supplied payment id
(``PAY1``/``PAY2`` ...) is only a label for one attempt.

Order of operations for a new instruction:

1. Write a local payment intent (unique per payable) and verify the payable /
   merchant / funding-source relationship.
2. Query the bank by the stable key *before* submitting anything.  Anything the
   bank already did is adopted, never repeated.
3. Submit only if the bank has no receipt for the key.
4. Attach the external receipt and write the local confirmation in one SQLite
   transaction, guarded by unique indexes.

Unknown or accepted bank results leave the intent pending: no second transfer
is allowed until a query yields a final result.  Rejected results unfreeze the
payable under the same key.
"""
import json
from . import storage as s
from . import bank as b

CITY = 'city_subsidy'
SENIOR = 'senior_reimbursement'
FUNDING_SOURCES = (CITY, SENIOR)
SUBSIDY_ACCOUNT = 'city-subsidy-account'
PAYEE_KIND = {CITY: 'merchant-payout', SENIOR: 'senior-reimbursement'}

class PayoutError(ValueError):
    def __init__(self, code, **detail):
        super().__init__(code)
        self.code = code
        self.detail = detail

def _terms(con, payable, merchant, funding_source):
    """Resolve and verify what this payable actually owes, and to whom."""
    doc = con.execute("SELECT state FROM documents WHERE id=? AND kind='payable-terms'",
                      (payable['id'] + ':terms',)).fetchone()
    if doc:
        raw = json.loads(doc['state'])
    else:
        # Legacy payable written before terms existed: the payable itself is
        # the only local evidence.  Its source defaults to the city path.
        raw = {
            'merchant': merchant,
            'payee': merchant,
            'amount': int(payable['amount']),
            'funding_source': CITY,
            'budget_account': SUBSIDY_ACCOUNT,
            'budget_amount': int(payable['amount']),
            'recourse': 'merchant',
        }
    terms = {k: raw.get(k) for k in ('merchant', 'payee', 'amount', 'funding_source',
                                     'budget_account', 'budget_amount', 'recourse')}
    terms['amount'] = int(terms['amount'])
    terms['budget_amount'] = int(terms['budget_amount'])
    terms['subject'] = payable['subject']
    terms['payable_id'] = payable['id']
    # Redemptions recorded before merchant capture name the owed merchant only
    # at payout time; the supplied merchant is then the expected relationship.
    merchant_known = raw.get('merchant_known', raw.get('merchant') not in (None, '', 'merchant-unassigned'))
    if not merchant_known:
        terms['merchant'] = merchant
        if terms['funding_source'] == CITY:
            terms['payee'] = merchant

    if merchant_known and terms['merchant'] != merchant:
        raise PayoutError('payable-merchant-mismatch', expected=terms['merchant'], supplied=merchant)
    if funding_source is not None and funding_source != terms['funding_source']:
        raise PayoutError('payable-funding-source-mismatch', expected=terms['funding_source'], supplied=funding_source)
    if terms['funding_source'] not in FUNDING_SOURCES:
        raise PayoutError('unknown-funding-source', supplied=terms['funding_source'])
    if terms['amount'] <= 0:
        raise PayoutError('nonpositive-payable')
    if terms['budget_amount'] < 0 or terms['budget_amount'] > terms['amount']:
        raise PayoutError('budget-exceeds-payment')
    return terms

def _ensure_intent(con, payable_id, merchant, funding_source, event_id):
    """Create the intent once, or resume the existing one for this payable."""
    con.execute('BEGIN IMMEDIATE')
    try:
        payable = con.execute("SELECT * FROM receipts WHERE id=? AND kind='merchant-payable'", (payable_id,)).fetchone()
        if not payable:
            con.rollback()
            raise PayoutError('no-payable')
        state = payable['state']
        terms = _terms(con, payable, merchant, funding_source)
        intent = s.intent_for(con, payable_id)
        if intent is not None:
            if intent['merchant'] != merchant or intent['funding_source'] != terms['funding_source']:
                con.rollback()
                raise PayoutError('intent-conflict', intent=intent['id'])
            if intent['state'] == 'rejected':
                con.execute("UPDATE payment_intents SET state='pending' WHERE id=?", (intent['id'],))
                con.commit()
                return s.intent_for(con, payable_id), terms
            con.commit()
            return intent, terms
        if state != 'open':
            con.rollback()
            raise PayoutError('payable-not-payable', state=state)
        intent_id = f"intent:{payable_id}"
        con.execute(
            'INSERT INTO payment_intents(id,payable_id,merchant,payee,amount,funding_source,budget_account,budget_amount,bank_key,state,created_event,receipt_id,raw,created_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (intent_id, payable_id, merchant, terms['payee'], terms['amount'], terms['funding_source'],
             terms['budget_account'], terms['budget_amount'], payable_id, 'pending', event_id, None,
             json.dumps(terms, ensure_ascii=False, sort_keys=True), s.now()))
        con.commit()
        return s.intent_for(con, payable_id), terms
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise

def _request(intent, payment_id):
    return {
        'idempotency_key': intent['bank_key'],
        'payment_id': payment_id,
        'payable_id': intent['payable_id'],
        'merchant': intent['merchant'],
        'payee': intent['payee'],
        'amount': int(intent['amount']),
    }

def _existing_evidence(con, intent):
    """Decide from external receipts already mirrored locally for this payable.

    Local evidence can outlive a lost bank file (or arrive from a previous
    process).  It is never silently replaced, and two non-conflicting payment
    receipts for one payable are a conflict.
    """
    rows = con.execute("SELECT * FROM external_payments WHERE payable_id=? ORDER BY created_at,receipt_id",
                       (intent['payable_id'],)).fetchall()
    payments = [r for r in rows if r['kind'] == 'payment']
    if not payments:
        return None
    conflicts = [r for r in payments if r['status'] == 'conflict']
    if conflicts:
        rec = dict(conflicts[0])
        return _freeze(con, intent, 'bank-conflict', rec['receipt_id'], json.loads(rec['raw']))
    distinct = {r['receipt_id'] for r in payments if r['status'] != 'rejected'}
    if len(distinct) > 1:
        rec = dict(payments[-1])
        return _freeze(con, intent, 'multiple-bank-receipts', rec['receipt_id'], json.loads(rec['raw']),
                       preserve_intent_state=True)
    settled_statuses = [r for r in payments if r['status'] != 'rejected']
    if not settled_statuses:
        # Every known receipt for this key was rejected: nothing moved.
        return _apply_bank_state(con, intent, json.loads(payments[-1]['raw']))
    rec = dict(settled_statuses[0])
    if int(rec['amount']) != int(intent['amount']) or rec['payee'] != intent['payee']:
        return _freeze(con, intent, 'bank-receipt-mismatch', rec['receipt_id'], json.loads(rec['raw']))
    status = rec['status']
    if status == 'transferred':
        return _post(con, intent, json.loads(rec['raw']))
    if status == 'rejected':
        return _apply_bank_state(con, intent, json.loads(rec['raw']))
    return _pending(con, intent, status if status in ('accepted', 'unknown') else 'unknown',
                    json.loads(rec['raw']))

def _freeze(con, intent, reason, receipt_id=None, raw=None, preserve_intent_state=False):
    """Freeze the payable while preserving every piece of external evidence."""
    if not con.in_transaction:
        con.execute('BEGIN IMMEDIATE')
    try:
        if not preserve_intent_state:
            con.execute("UPDATE payment_intents SET state='frozen' WHERE id=?", (intent['id'],))
        con.execute("UPDATE receipts SET state='frozen' WHERE id=?", (intent['payable_id'],))
        if raw is not None and receipt_id:
            existing = con.execute("SELECT * FROM external_payments WHERE receipt_id=?", (receipt_id,)).fetchone()
            if not existing:
                con.execute(
                    'INSERT OR IGNORE INTO external_payments(receipt_id,payable_id,intent_id,kind,idempotency_key,payment_id,merchant,payee,amount,status,raw,created_at) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (receipt_id, intent['payable_id'], intent['id'], raw.get('kind', 'payment'), intent['bank_key'],
                     raw.get('payment_id', '') or '', raw.get('merchant') or '', raw.get('payee') or '',
                     int(raw.get('amount', 0)), 'conflict',
                     json.dumps(raw, ensure_ascii=False, sort_keys=True), s.now()))
        s.attempt(con, intent['id'], None, 'conflict', reason, {'bank_receipt': receipt_id})
        con.commit()
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    return {'state': 'frozen', 'reason': reason, 'intent': intent['id'], 'bank_receipt': receipt_id}

def _post(con, intent, receipt):
    """Attach one bank receipt to one payable and confirm locally, exactly once."""
    amount = int(receipt.get('amount', 0))
    payee = receipt.get('payee') or receipt.get('merchant')
    if amount != int(intent['amount']) or payee != intent['payee']:
        return _freeze(con, intent, 'bank-receipt-mismatch', receipt.get('receipt_id'), receipt)

    con.execute('BEGIN IMMEDIATE')
    try:
        # Another thread/process may have confirmed this payable while we were
        # waiting for the write lock.  The confirmation wins; do not add a
        # second local receipt or budget posting.
        fresh = con.execute("SELECT * FROM payment_intents WHERE id=?", (intent['id'],)).fetchone()
        if fresh is not None and fresh['state'] in ('settled', 'reversed'):
            con.rollback()
            return {'state': 'already', 'intent': intent['id'], 'receipt': fresh['receipt_id']}
        held = con.execute("SELECT * FROM external_payments WHERE receipt_id=?", (receipt['receipt_id'],)).fetchone()
        if held is not None and held['payable_id'] != intent['payable_id']:
            # A bank receipt may belong to only one payable.  Preserve the
            # original attachment and freeze this second claim.
            con.rollback()
            result = _freeze(con, intent, 'bank-receipt-already-assigned', receipt.get('receipt_id'), receipt,
                             preserve_intent_state=True)
            return result
        if held is None:
            con.execute(
                'INSERT INTO external_payments(receipt_id,payable_id,intent_id,kind,idempotency_key,payment_id,merchant,payee,amount,status,raw,created_at) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (receipt['receipt_id'], intent['payable_id'], intent['id'], receipt.get('kind', 'payment'),
                 intent['bank_key'], receipt.get('payment_id') or '', receipt.get('merchant') or intent['merchant'],
                 payee, amount, receipt.get('status', 'transferred'),
                 json.dumps(receipt, ensure_ascii=False, sort_keys=True), s.now()))
        local_id = receipt.get('payment_id') or receipt['receipt_id']
        conflicting = con.execute("SELECT id FROM receipts WHERE id=?", (local_id,)).fetchone()
        if conflicting is not None and conflicting['id'] != intent['payable_id']:
            con.rollback()
            return _freeze(con, intent, 'local-receipt-id-conflict', receipt.get('receipt_id'), receipt,
                           preserve_intent_state=True)
        con.execute('INSERT OR IGNORE INTO receipts VALUES(?,?,?,?,?)',
                    (local_id, intent['payee'], PAYEE_KIND[intent['funding_source']], 'paid', amount))
        con.execute("UPDATE receipts SET state='settled' WHERE id=?", (intent['payable_id'],))
        con.execute("UPDATE payment_intents SET state='settled',receipt_id=? WHERE id=?", (local_id, intent['id']))
        s.budget(con, intent['budget_account'], intent['payable_id'], intent['id'], receipt['receipt_id'],
                 intent['funding_source'], 'subsidy-spend', int(intent['budget_amount']),
                 memo=f"payee={intent['payee']}")
        s.attempt(con, intent['id'], local_id, 'posted', 'transferred', {'bank_receipt': receipt['receipt_id']})
        con.commit()
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    return {'state': 'settled', 'intent': intent['id'], 'bank_receipt': receipt['receipt_id'], 'receipt': local_id}

def _pending(con, intent, outcome, receipt=None):
    state = 'unknown' if outcome == 'unknown' else 'pending'
    con.execute('BEGIN IMMEDIATE')
    try:
        con.execute("UPDATE payment_intents SET state=? WHERE id=?", (state, intent['id']))
        # A pending/unknown bank result is not "unpaid": take the payable out of
        # the open queue so a status-only retry cannot start a second transfer.
        con.execute("UPDATE receipts SET state='pending' WHERE id=? AND state IN ('open','pending')",
                    (intent['payable_id'],))
        if receipt is not None and receipt.get('receipt_id'):
            # Mirror the pending bank receipt locally.  If the bank file is
            # later lost, this evidence still prevents a second submission.
            existing = con.execute("SELECT 1 FROM external_payments WHERE receipt_id=?",
                                   (receipt['receipt_id'],)).fetchone()
            if not existing:
                con.execute(
                    'INSERT OR IGNORE INTO external_payments(receipt_id,payable_id,intent_id,kind,idempotency_key,payment_id,merchant,payee,amount,status,raw,created_at) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (receipt['receipt_id'], intent['payable_id'], intent['id'], receipt.get('kind', 'payment'),
                     intent['bank_key'], receipt.get('payment_id') or '', receipt.get('merchant') or intent['merchant'],
                     receipt.get('payee') or intent['payee'], int(receipt.get('amount', 0)),
                     receipt.get('status', 'unknown'),
                     json.dumps(receipt, ensure_ascii=False, sort_keys=True), s.now()))
        s.attempt(con, intent['id'], receipt.get('payment_id') if receipt else None, 'query', outcome,
                  {'bank_receipt': receipt.get('receipt_id') if receipt else None,
                   'bank_status': receipt.get('status') if receipt else None})
        con.commit()
    except Exception:
        if con.in_transaction:
            con.rollback()
        raise
    return {'state': state, 'intent': intent['id'], 'bank': receipt}

def _apply_bank_state(con, intent, receipt):
    status = receipt.get('status')
    if status == 'conflict':
        return _freeze(con, intent, 'bank-conflict', receipt.get('receipt_id'), receipt)
    if status == 'transferred':
        return _post(con, intent, receipt)
    if status == 'reversed':
        # The bank shows a compensating transaction for this key.  Record it
        # as external evidence and learn the confirmed payout from the mirror;
        # never treat it as a new payable.
        con.execute('BEGIN IMMEDIATE')
        try:
            existing = con.execute("SELECT 1 FROM external_payments WHERE receipt_id=?",
                                   (receipt.get('receipt_id'),)).fetchone()
            if not existing:
                con.execute(
                    'INSERT OR IGNORE INTO external_payments(receipt_id,payable_id,intent_id,kind,idempotency_key,payment_id,merchant,payee,amount,status,raw,reversal_of,created_at) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (receipt.get('receipt_id'), intent['payable_id'], intent['id'], 'reversal', intent['bank_key'],
                     receipt.get('payment_id') or '', receipt.get('merchant') or intent['merchant'],
                     receipt.get('payee') or intent['payee'], int(receipt.get('amount', 0)), 'transferred',
                     json.dumps(receipt, ensure_ascii=False, sort_keys=True), receipt.get('reversal_of'), s.now()))
            con.commit()
        except Exception:
            if con.in_transaction:
                con.rollback()
            raise
        return {'state': 'reversed', 'intent': intent['id'], 'bank_receipt': receipt.get('receipt_id')}
    if status == 'rejected':
        # The bank definitively refused: no money moved.  The payable is
        # unfrozen only if it was still held by this intent.
        con.execute('BEGIN IMMEDIATE')
        try:
            con.execute("UPDATE payment_intents SET state='rejected' WHERE id=?", (intent['id'],))
            con.execute("UPDATE receipts SET state='open' WHERE id=? AND state='pending'", (intent['payable_id'],))
            s.attempt(con, intent['id'], receipt.get('payment_id'), 'unfreeze', 'rejected',
                      {'bank_receipt': receipt.get('receipt_id')})
            con.commit()
        except Exception:
            if con.in_transaction:
                con.rollback()
            raise
        return {'state': 'rejected', 'intent': intent['id'], 'bank': receipt}
    # accepted / unknown / anything else: the bank may or may not have moved
    # money.  Never send a second transfer; query again later.
    return _pending(con, intent, 'unknown' if status not in ('accepted', 'unknown') else status, receipt)

def _inspect_history(con, intent, history):
    """Return a decision derived from the bank's receipts for one key, or None."""
    payments = [r for r in history if r.get('kind', 'payment') == 'payment']
    conflicts = [r for r in payments if r.get('status') == 'conflict']
    if conflicts:
        return _freeze(con, intent, 'bank-conflict', conflicts[0].get('receipt_id'), conflicts[0])
    for receipt in reversed(payments):
        if receipt.get('status') in ('transferred', 'accepted', 'unknown', 'rejected'):
            return _apply_bank_state(con, intent, receipt)
    return None

def pay(db_path, bank_path, payment_id, merchant, payable_id, fail_after_bank=False,
        funding_source=None, bank_status='transferred'):
    """Issue (or resume) one payout instruction for ``payable_id``.

    ``bank_status`` is only used by offline fixtures/tests to model the answer
    of the first submission; recovery always reads the stored bank receipt.
    """
    con = s.connect(db_path)
    try:
        intent, _terms_used = _ensure_intent(con, payable_id, merchant, funding_source, payment_id)
        if intent['state'] in ('settled', 'reversed'):
            return {'state': 'already', 'intent': intent['id'], 'receipt': intent['receipt_id']}
        if intent['state'] == 'frozen':
            return {'state': 'frozen', 'intent': intent['id'], 'bank_receipt': None}

        # Anything the bank already knows wins over a new submission; local
        # mirrored evidence is equally authoritative when the bank file was
        # lost or replaced.
        history = [r for r in b.rows(bank_path)
                   if r.get('idempotency_key', r.get('payment_id')) == intent['bank_key']]
        decided = _inspect_history(con, intent, history)
        if decided is None:
            decided = _existing_evidence(con, intent)
        if decided is not None:
            return decided

        receipt = b.transfer(bank_path, _request(intent, payment_id), status=bank_status)
        if fail_after_bank:
            raise RuntimeError('connection_lost_after_bank')
        return _apply_bank_state(con, s.intent_for(con, payable_id), receipt)
    finally:
        con.close()

def recover(db_path, bank_path, payable_id=None):
    """Reconcile local intents with the bank's final results.  Safe to replay."""
    con = s.connect(db_path)
    try:
        if payable_id is not None:
            rows = con.execute("SELECT * FROM payment_intents WHERE payable_id=?", (payable_id,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM payment_intents WHERE state IN ('pending','unknown','rejected') "
                               "ORDER BY created_at,id").fetchall()
        results = []
        for intent in rows:
            history = [r for r in b.rows(bank_path)
                       if r.get('idempotency_key', r.get('payment_id')) == intent['bank_key']]
            if not history:
                results.append({'state': 'no-bank-receipt', 'intent': intent['id']})
                continue
            fresh = s.intent_for(con, intent['payable_id'])
            decided = _inspect_history(con, fresh, history)
            if decided is None:
                decided = _existing_evidence(con, fresh)
            results.append(decided if decided is not None else {'state': 'pending', 'intent': intent['id']})
        return results
    finally:
        con.close()

def reverse(db_path, bank_path, payment_id, payable_id, reason='reversal'):
    """Reverse a completed payout with a new bank transaction and new postings."""
    con = s.connect(db_path)
    try:
        intent = s.intent_for(con, payable_id)
        if intent is None or intent['state'] != 'settled':
            raise PayoutError('no-settled-payout', payable_id=payable_id)
        request = {
            'idempotency_key': intent['bank_key'],
            'payment_id': payment_id,
            'payable_id': payable_id,
            'merchant': intent['merchant'],
            'payee': intent['payee'],
            'amount': int(intent['amount']),
        }
        receipt = b.reverse(bank_path, request)

        con.execute('BEGIN IMMEDIATE')
        try:
            held = con.execute("SELECT * FROM external_payments WHERE receipt_id=?", (receipt['receipt_id'],)).fetchone()
            if held is None:
                con.execute(
                    'INSERT INTO external_payments(receipt_id,payable_id,intent_id,kind,idempotency_key,payment_id,merchant,payee,amount,status,raw,reversal_of,created_at) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (receipt['receipt_id'], payable_id, intent['id'], 'reversal', intent['bank_key'],
                     receipt.get('payment_id') or '', intent['merchant'], intent['payee'],
                     int(receipt.get('amount', 0)), receipt.get('status', 'transferred'),
                     json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                     receipt.get('reversal_of'), s.now()))
            local_id = receipt.get('payment_id') or receipt['receipt_id']
            con.execute('INSERT OR IGNORE INTO receipts VALUES(?,?,?,?,?)',
                        (local_id, intent['payee'], 'payout-reversal', 'reversed',
                         abs(int(receipt.get('amount', 0)))))
            con.execute("UPDATE receipts SET state='reversed' WHERE id=?", (intent['receipt_id'],))
            con.execute("UPDATE receipts SET state='reversed' WHERE id=?", (payable_id,))
            con.execute("UPDATE payment_intents SET state='reversed' WHERE id=?", (intent['id'],))
            s.budget(con, intent['budget_account'], payable_id, intent['id'], receipt['receipt_id'],
                     intent['funding_source'], 'subsidy-spend-reversal', -abs(int(intent['budget_amount'])),
                     memo=reason)
            s.attempt(con, intent['id'], local_id, 'reversed', reason,
                      {'bank_receipt': receipt['receipt_id']})
            con.commit()
        except Exception:
            if con.in_transaction:
                con.rollback()
            raise
        return {'state': 'reversed', 'intent': intent['id'],
                'bank_receipt': receipt['receipt_id'], 'receipt': local_id}
    finally:
        con.close()

def trace(db_path, payable_id):
    con = s.connect(db_path)
    try:
        return s.payment_trace(con, payable_id)
    finally:
        con.close()

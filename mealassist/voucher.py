"""Paper-ticket batch intake and globally unique voucher assignment.

OCR confidence only orders candidates; it never proves that a voucher was
issued to a ticket.  Assignment therefore solves over the whole batch at once:

* one issued voucher belongs to at most one confirmed paper ticket;
* one pickup (one meal) is redeemed with at most one voucher;
* a manual signature that cannot be withdrawn is a hard constraint, not a hint.

The solver maximises the number of honoured paper tickets first, then the
issued amount, then the OCR candidate order.  Each successful run is a new,
immutable allocation version.  Images, signatures, original images and older
allocation versions are never overwritten.
"""
import json
import sqlite3
from collections import deque

from . import storage as s
from . import ticket_scan

NAME = '纸票采入与分配'

FULL_REDEMPTION = 'full-redemption'
RETAIN_SIGNATURE = 'retain-signature'

_CARDINALITY = 10 ** 9
_AMOUNT_WEIGHT = 100
_RANK_WEIGHT = 1


# ---------------------------------------------------------------------------
# Legacy per-image claim.  This is provisional OCR evidence only: it is never
# stored and can never create a merchant payable on its own.
# ---------------------------------------------------------------------------

def claim(ticket):
    choice = ticket_scan.choose(ticket['ocr_candidates'])
    return {'ticket_id': ticket['ticket_id'], 'voucher_id': choice['voucher_id'],
            'amount': choice['amount'], 'state': 'accepted'}


# ---------------------------------------------------------------------------
# Evidence normalisation (pure; shared by import and replay).
# ---------------------------------------------------------------------------

def _int(value, reason):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(reason)


def _confidence(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError('invalid-confidence')
    if not 0.0 <= result <= 1.0:
        raise ValueError('invalid-confidence')
    return result


def _normalise_image(ticket_id, image, default_source):
    if 'image_hash' in image:
        image_hash = str(image['image_hash']).strip()
    elif 'image' in image:
        image_hash = ticket_scan.image_hash(image['image'])
    else:
        raise ValueError('missing-image-hash')
    if not image_hash:
        raise ValueError('missing-image-hash')
    received_at = _int(image.get('received_at', 0), 'invalid-image-received-at')
    source = str(image.get('source', default_source))
    candidates = image.get('candidates')
    if candidates is None:
        raise ValueError('missing-ocr-candidates')
    normalised = []
    for candidate in candidates:
        voucher_id = str(candidate.get('voucher_id', '')).strip()
        if not voucher_id:
            raise ValueError('missing-voucher-id')
        amount = _int(candidate.get('amount'), 'invalid-candidate-amount')
        if amount <= 0:
            raise ValueError('invalid-candidate-amount')
        chars = ticket_scan.normalize_chars(candidate.get('chars', ''))
        if not chars:
            raise ValueError('candidate-missing-characters')
        normalised.append({'voucher_id': voucher_id, 'amount': amount,
                           'confidence': _confidence(candidate.get('confidence', 0.0)),
                           'chars': chars})
    if not normalised:
        raise ValueError('no_ocr_candidate')
    return {'image_hash': image_hash, 'received_at': received_at, 'source': source,
            'ticket_id': ticket_id, 'candidates': normalised}


def _normalise_signature(ticket_id, signature):
    signature_id = str(signature.get('signature_id', '')).strip()
    if not signature_id:
        raise ValueError('missing-signature-id')
    voucher_id = str(signature.get('voucher_id', '')).strip()
    if not voucher_id:
        raise ValueError('missing-signature-voucher')
    sealed_period = str(signature.get('sealed_period', ''))
    sealed = bool(signature.get('sealed', False)) or bool(sealed_period) \
        or signature.get('revocable') is False
    return {'signature_id': signature_id, 'ticket_id': ticket_id,
            'voucher_id': voucher_id,
            'state': str(signature.get('state', 'pending_review')),
            'sealed': sealed, 'sealed_period': sealed_period,
            'revocable': not sealed,
            'signer': signature.get('signer'), 'issuer': signature.get('issuer')}


def normalise_import(data):
    """Validate an import payload completely before any row is written."""
    batch_id = str(data.get('batch_id', '')).strip()
    if not batch_id:
        raise ValueError('missing-batch-id')
    book = {}
    for entry in data.get('book', []):
        voucher_id = str(entry.get('voucher_id', '')).strip()
        if not voucher_id:
            raise ValueError('missing-voucher-id')
        amount = _int(entry.get('amount'), 'invalid-issued-amount')
        if amount <= 0:
            raise ValueError('invalid-issued-amount')
        item = {'voucher_id': voucher_id, 'amount': amount,
                'book': str(entry.get('book', '')),
                'sealed_bag': str(entry.get('sealed_bag', ''))}
        if voucher_id in book and book[voucher_id] != item:
            raise ValueError('issued-voucher-conflict')
        book[voucher_id] = item
    tickets = []
    seen = set()
    for raw in data.get('tickets', []):
        ticket_id = str(raw.get('ticket_id', '')).strip()
        if not ticket_id or ticket_id in seen:
            raise ValueError('missing-or-duplicate-ticket-id')
        seen.add(ticket_id)
        pickup_id = str(raw.get('pickup_id', '')).strip()
        recipient = str(raw.get('recipient', '')).strip()
        images = raw.get('images')
        if images is None:
            if 'image_hash' in raw or 'image' in raw:
                images = [raw]
            else:
                images = []
        signatures = [_normalise_signature(ticket_id, item)
                      for item in raw.get('signatures', [])]
        if not images and not signatures:
            raise ValueError('missing-image-hash')
        # A signature-only correction attaches to an already imported ticket;
        # a ticket that carries images must also carry pickup and recipient.
        if images and (not pickup_id or not recipient):
            raise ValueError('missing-pickup-or-recipient')
        normalised_images = [_normalise_image(ticket_id, image,
                                              str(raw.get('source', '')))
                             for image in images]
        if len({item['signature_id'] for item in signatures}) != len(signatures):
            raise ValueError('duplicate-signature-in-import')
        tickets.append({'ticket_id': ticket_id, 'pickup_id': pickup_id,
                        'recipient': recipient, 'images': normalised_images,
                        'signatures': signatures})
    if not tickets:
        raise ValueError('empty-ticket-batch')
    return {'batch_id': batch_id, 'book': book, 'tickets': tickets}


def normalise_approvals(value):
    """A signature is withdrawable only with both signer and issuer approval."""
    result = {}
    if not value:
        return result
    if isinstance(value, dict):
        items = list(value.items())
    else:
        items = [((item if isinstance(item, str)
                   else item.get('signature_id') or item.get('id')), item)
                 for item in value]
    for signature_id, info in items:
        if not signature_id:
            continue
        if isinstance(info, dict):
            approved = bool(info.get('signer')) and bool(info.get('issuer'))
        else:
            approved = bool(info)
        result[str(signature_id)] = approved
    return result


# ---------------------------------------------------------------------------
# Append-only persistence of ticket evidence.
# ---------------------------------------------------------------------------

def _store_book(con, book):
    for voucher_id, item in sorted(book.items()):
        row = con.execute('SELECT * FROM issued_vouchers WHERE voucher_id=?',
                          (voucher_id,)).fetchone()
        if row:
            if int(row['amount']) != item['amount']:
                raise ValueError('issued-voucher-conflict')
            continue
        con.execute('INSERT INTO issued_vouchers VALUES(?,?,?,?,?)',
                    (voucher_id, item['amount'], item['book'], item['sealed_bag'],
                     s.canonical(item)))


def _store_pickup(con, pickup_id, recipient):
    row = con.execute('SELECT * FROM ticket_pickups WHERE pickup_id=?',
                      (pickup_id,)).fetchone()
    if row and row['subject'] != recipient:
        raise ValueError('pickup-recipient-conflict')
    if not row:
        con.execute('INSERT INTO ticket_pickups VALUES(?,?,?,?)',
                    (pickup_id, recipient, 'picked-up',
                     s.canonical({'pickup_id': pickup_id, 'subject': recipient})))


def _store_image(con, batch_id, ticket, image):
    raw = s.canonical(image)
    existing = con.execute(
        'SELECT * FROM ticket_images WHERE batch_id=? AND ticket_id=? AND image_hash=?',
        (batch_id, ticket['ticket_id'], image['image_hash'])).fetchone()
    if existing:
        if existing['raw'] != raw:
            raise ValueError('image-hash-evidence-conflict')
        return False
    row = con.execute('SELECT COALESCE(MAX(version),0) v FROM ticket_images '
                      'WHERE batch_id=? AND ticket_id=?',
                      (batch_id, ticket['ticket_id'])).fetchone()
    version = int(row['v']) + 1
    con.execute('INSERT INTO ticket_images VALUES(?,?,?,?,?,?,?,?,?)',
                (batch_id, ticket['ticket_id'], image['image_hash'], ticket['pickup_id'],
                 ticket['recipient'], image['received_at'], version,
                 image['source'], raw))
    for rank, candidate in enumerate(ticket_scan.ordered(image['candidates'])):
        con.execute('INSERT INTO ticket_candidates VALUES(?,?,?,?,?,?,?,?)',
                    (batch_id, ticket['ticket_id'], image['image_hash'], rank,
                     candidate['voucher_id'], candidate['amount'],
                     candidate['confidence'], candidate['chars']))
    return True


def _store_signature(con, batch_id, signature):
    prior = con.execute(
        'SELECT * FROM ticket_signatures WHERE batch_id=? AND signature_id=? '
        'ORDER BY version DESC LIMIT 1',
        (batch_id, signature['signature_id'])).fetchone()
    raw = s.canonical(signature)
    version = int(prior['version']) + 1 if prior else 1
    if prior and prior['raw'] == raw:
        return False
    con.execute('INSERT INTO ticket_signatures VALUES(?,?,?,?,?,?,?,?,?)',
                (batch_id, signature['signature_id'], version, signature['ticket_id'],
                 signature['voucher_id'], signature['state'],
                 0 if signature['sealed'] else 1,
                 signature['sealed_period'], raw))
    return True


def import_batch(con, event):
    try:
        payload = normalise_import(event.get('data', {}))
    except ValueError as error:
        return s.decide(con, event, NAME, 'blocked', str(error), 'blocked')
    try:
        _store_book(con, payload['book'])
        incoming = {ticket['ticket_id'] for ticket in payload['tickets'] if ticket['images']}
        for ticket in payload['tickets']:
            if not ticket['signatures']:
                continue
            known = ticket['ticket_id'] in incoming or con.execute(
                'SELECT 1 FROM ticket_images WHERE batch_id=? AND ticket_id=? LIMIT 1',
                (payload['batch_id'], ticket['ticket_id'])).fetchone()
            if not known:
                raise ValueError('signature-for-unknown-ticket')
        images = signatures = 0
        for ticket in payload['tickets']:
            if ticket['images']:
                _store_pickup(con, ticket['pickup_id'], ticket['recipient'])
            for image in ticket['images']:
                images += _store_image(con, payload['batch_id'], ticket, image)
        for ticket in payload['tickets']:
            for signature in ticket['signatures']:
                signatures += _store_signature(con, payload['batch_id'], signature)
    except ValueError as error:
        con.rollback()
        return s.decide(con, event, NAME, 'blocked', str(error), 'blocked')
    except sqlite3.IntegrityError as error:
        con.rollback()
        return s.decide(con, event, NAME, 'blocked',
                        f'evidence-integrity:{error}', 'blocked')
    outcome = 'imported' if images or signatures else 'imported-duplicate'
    return s.decide(con, event, NAME, outcome,
                    f'images={images};signatures={signatures};'
                    f'tickets={len(payload["tickets"])}', outcome)


# ---------------------------------------------------------------------------
# Evidence readback.
# ---------------------------------------------------------------------------

def load_evidence(con, batch_id):
    rows = con.execute(
        'SELECT * FROM ticket_images WHERE batch_id=? '
        'ORDER BY ticket_id, received_at, version, image_hash', (batch_id,)).fetchall()
    latest = {}
    for row in rows:
        latest[row['ticket_id']] = row  # ordered: the last row is newest evidence
    if not latest:
        return None
    book = {row['voucher_id']: {'voucher_id': row['voucher_id'],
                                'amount': int(row['amount']),
                                'book': row['book'], 'sealed_bag': row['sealed_bag']}
            for row in con.execute('SELECT * FROM issued_vouchers ORDER BY voucher_id')}
    tickets = []
    for ticket_id in sorted(latest):
        image = latest[ticket_id]
        # The ticket's pickup is its original image version; each image row
        # carries the pickup that the ticket image was matched with.
        first = con.execute('SELECT * FROM ticket_images WHERE batch_id=? AND ticket_id=? '
                            'ORDER BY version LIMIT 1', (batch_id, ticket_id)).fetchone()
        pickup_id = first['pickup_id'] if first else image['pickup_id']
        recipient = first['recipient'] if first else image['recipient']
        pickup = con.execute('SELECT * FROM ticket_pickups WHERE pickup_id=?',
                             (pickup_id,)).fetchone()
        candidates = [dict(row) for row in con.execute(
            'SELECT ocr_rank, voucher_id, amount, confidence, chars '
            'FROM ticket_candidates WHERE batch_id=? AND ticket_id=? AND image_hash=? '
            'ORDER BY ocr_rank',
            (batch_id, ticket_id, image['image_hash']))]
        signatures = []
        for sig in con.execute(
                'SELECT * FROM ticket_signatures WHERE batch_id=? AND ticket_id=? '
                'AND version=(SELECT MAX(x.version) FROM ticket_signatures x '
                'WHERE x.batch_id=ticket_signatures.batch_id '
                'AND x.signature_id=ticket_signatures.signature_id) '
                'ORDER BY signature_id', (batch_id, ticket_id)):
            signatures.append({'signature_id': sig['signature_id'],
                               'voucher_id': sig['voucher_id'],
                               'state': sig['state'],
                               'sealed': not bool(sig['revocable']),
                               'sealed_period': sig['sealed_period']})
        tickets.append({'ticket_id': ticket_id, 'pickup_id': pickup_id,
                        'recipient': recipient,
                        'image': {'image_hash': image['image_hash'],
                                  'received_at': image['received_at'],
                                  'version': image['version'],
                                  'source': image['source']},
                        'pickup_state': pickup['state'] if pickup else 'missing',
                        'candidates': candidates, 'signatures': signatures})
    return {'batch_id': batch_id, 'book': book, 'tickets': tickets}


# ---------------------------------------------------------------------------
# Deterministic min-cost max-flow.
# ---------------------------------------------------------------------------

class _Flow:
    def __init__(self, size):
        self.graph = [[] for _ in range(size)]

    def add(self, u, v, cap, cost):
        forward = [v, cap, cost, len(self.graph[v])]
        backward = [u, 0, -cost, len(self.graph[u])]
        self.graph[u].append(forward)
        self.graph[v].append(backward)
        return forward

    def run(self, source, sink):
        while True:
            dist = [None] * len(self.graph)
            prev_node = [-1] * len(self.graph)
            prev_edge = [-1] * len(self.graph)
            dist[source] = 0
            queue = deque([source])
            queued = {source}
            while queue:
                u = queue.popleft()
                queued.discard(u)
                for index, edge in enumerate(self.graph[u]):
                    v, cap, cost, _ = edge
                    if cap <= 0:
                        continue
                    candidate = dist[u] + cost
                    if dist[v] is None or candidate < dist[v]:
                        dist[v] = candidate
                        prev_node[v] = u
                        prev_edge[v] = index
                        if v not in queued:
                            queue.append(v)
                            queued.add(v)
            if dist[sink] is None:
                return
            v = sink
            while v != source:
                u = prev_node[v]
                edge = self.graph[u][prev_edge[v]]
                edge[1] -= 1
                self.graph[v][edge[3]][1] += 1
                v = u


# ---------------------------------------------------------------------------
# Signature constraints and global assignment.
# ---------------------------------------------------------------------------

def _signature_decision(signature, approvals):
    """Return ('free'|'pinned', reason) for one latest signature version."""
    state = str(signature.get('state', 'pending_review'))
    if state in ('revoked', 'void', 'cancelled', 'withdrawn'):
        return 'free', 'signature-already-withdrawn'
    if signature.get('sealed'):
        return 'pinned', 'signature-sealed-period'
    if approvals.get(signature['signature_id']):
        return 'free', 'signature-withdrawn-by-signer-and-issuer'
    return 'pinned', 'signature-pending-review'


def build_assignment(evidence, approvals=None, effective_at=0):
    """Pure, replayable global assignment over one batch's latest evidence."""
    approvals = normalise_approvals(approvals)
    book = evidence['book']
    tickets = sorted(evidence['tickets'], key=lambda item: item['ticket_id'])
    voucher_owner = {}   # voucher_id -> ticket_id
    pickup_owner = {}    # pickup_id -> ticket_id
    rows = []
    conflicts = []
    protocol = FULL_REDEMPTION

    def edge(ticket_id, kind, voucher_id=None, against=None, detail=''):
        conflicts.append({'ticket_id': ticket_id, 'voucher_id': voucher_id,
                          'kind': kind, 'against': against, 'detail': detail})

    def pin(ticket, signature, reason):
        nonlocal protocol
        ticket_id = ticket['ticket_id']
        voucher_id = signature['voucher_id']
        protocol = RETAIN_SIGNATURE
        voucher = book.get(voucher_id)
        if voucher is None:
            edge(ticket_id, 'signed-voucher-not-in-issued-book', voucher_id,
                 detail=reason)
            return
        if voucher_id in voucher_owner:
            edge(ticket_id, 'voucher-held-by-other-ticket', voucher_id,
                 against=voucher_owner[voucher_id], detail=reason)
            return
        if ticket['pickup_id'] in pickup_owner:
            edge(ticket_id, 'pickup-already-allocated', voucher_id,
                 against=pickup_owner[ticket['pickup_id']], detail=reason)
            return
        voucher_owner[voucher_id] = ticket_id
        pickup_owner[ticket['pickup_id']] = ticket_id
        pinned_tickets.add(ticket_id)
        rows.append({'ticket_id': ticket_id, 'voucher_id': voucher_id,
                     'pickup_id': ticket['pickup_id'], 'recipient': ticket['recipient'],
                     'amount': int(voucher['amount']), 'state': 'confirmed',
                     'reason': reason, 'source': 'signature',
                     'signature_id': signature['signature_id'],
                     'image_hash': ticket.get('image', {}).get('image_hash'),
                     'confidence': None, 'ocr_rank': None})

    # 1. Non-withdrawable signatures are hard constraints.  They are solved
    #    before the optimiser and are never traded for a better batch total.
    #    A ticket whose signature cannot be honoured is held for review; it is
    #    not re-read under a different voucher while the signature stands.
    pinned_tickets = set()
    for ticket in tickets:
        for signature in ticket['signatures']:
            decision, reason = _signature_decision(signature, approvals)
            if decision == 'pinned':
                pinned_tickets.add(ticket['ticket_id'])
                pin(ticket, signature, reason)

    # 2. Validate every OCR edge against the issued book.  Confidence only
    #    ranks candidates; issuance proof is the book's voucher and amount.
    validated = {}
    for ticket in tickets:
        edges = []
        seen = set()
        for candidate in ticket['candidates']:
            voucher_id = candidate['voucher_id']
            if voucher_id in seen:
                continue
            seen.add(voucher_id)
            ok, reason = ticket_scan.validate_candidate(candidate,
                                                        book.get(voucher_id))
            edges.append({'candidate': candidate, 'valid': ok, 'reason': reason})
        validated[ticket['ticket_id']] = edges

    # 3. Global match of the remaining tickets.  A flow token per pickup group
    #    enforces "one meal, one voucher" inside the optimiser itself.
    matchable = []
    for ticket in tickets:
        ticket_id = ticket['ticket_id']
        if ticket_id in pinned_tickets:
            continue
        if ticket['pickup_id'] in pickup_owner:
            continue
        usable = [item for item in validated[ticket_id]
                  if item['valid'] and item['candidate']['voucher_id'] not in voucher_owner]
        if usable:
            matchable.append(ticket)

    groups = {}
    for ticket in matchable:
        groups.setdefault(ticket['pickup_id'], []).append(ticket)
    next_node = [1]
    def node():
        value = next_node[0]
        next_node[0] += 1
        return value
    source = 0
    group_nodes = {group_id: node() for group_id in sorted(groups)}
    ticket_nodes = {ticket['ticket_id']: node() for ticket in matchable}
    voucher_ids = set()
    for ticket in matchable:
        for item in validated[ticket['ticket_id']]:
            if item['valid'] and item['candidate']['voucher_id'] not in voucher_owner:
                voucher_ids.add(item['candidate']['voucher_id'])
    voucher_nodes = {voucher_id: node() for voucher_id in sorted(voucher_ids)}
    sink = next_node[0]
    flow = _Flow(sink + 1)
    for group_id, members in groups.items():
        flow.add(source, group_nodes[group_id], 1, 0)
        for ticket in members:
            flow.add(group_nodes[group_id], ticket_nodes[ticket['ticket_id']], 1, 0)
    ticket_edges = []
    for ticket in matchable:
        ticket_id = ticket['ticket_id']
        used = set()
        for item in validated[ticket_id]:
            candidate = item['candidate']
            voucher_id = candidate['voucher_id']
            if not item['valid'] or voucher_id not in voucher_nodes or voucher_id in used:
                continue
            used.add(voucher_id)
            rank = int(candidate.get('ocr_rank', 0))
            benefit = _CARDINALITY + int(candidate['amount']) * _AMOUNT_WEIGHT \
                - rank * _RANK_WEIGHT
            ticket_edges.append((ticket_id, voucher_id,
                                 flow.add(ticket_nodes[ticket_id],
                                          voucher_nodes[voucher_id], 1, -benefit)))
    for voucher_id in sorted(voucher_nodes):
        flow.add(voucher_nodes[voucher_id], sink, 1, 0)
    flow.run(source, sink)

    matched = {}
    for ticket_id, voucher_id, forward in ticket_edges:
        if forward[1] == 0 and ticket_id not in matched:
            matched[ticket_id] = voucher_id

    by_id = {ticket['ticket_id']: ticket for ticket in tickets}
    for ticket_id, voucher_id in sorted(matched.items()):
        ticket = by_id[ticket_id]
        candidate = next(item['candidate'] for item in validated[ticket_id]
                         if item['candidate']['voucher_id'] == voucher_id)
        voucher_owner[voucher_id] = ticket_id
        pickup_owner[ticket['pickup_id']] = ticket_id
        rows.append({'ticket_id': ticket_id, 'voucher_id': voucher_id,
                     'pickup_id': ticket['pickup_id'],
                     'recipient': ticket['recipient'],
                     'amount': int(book[voucher_id]['amount']), 'state': 'confirmed',
                     'reason': 'globally-unique-voucher', 'source': 'ocr-global',
                     'signature_id': None,
                     'image_hash': ticket.get('image', {}).get('image_hash'),
                     'confidence': candidate['confidence'],
                     'ocr_rank': candidate.get('ocr_rank')})

    # 4. Report every unallocated ticket with the conflict edges that blocked
    #    it.  Nothing is dropped silently.
    allocated = {row['ticket_id'] for row in rows}
    for ticket in tickets:
        ticket_id = ticket['ticket_id']
        if ticket_id in allocated:
            continue
        for item in validated[ticket_id]:
            candidate = item['candidate']
            voucher_id = candidate['voucher_id']
            if not item['valid']:
                edge(ticket_id, 'candidate-rejected', voucher_id, detail=item['reason'])
            elif voucher_id in voucher_owner:
                holder = voucher_owner[voucher_id]
                kind = 'voucher-held-by-signature' if holder in pinned_tickets \
                    else 'voucher-claimed-by-other-ticket'
                edge(ticket_id, kind, voucher_id, against=holder,
                     detail='one-issued-voucher-per-ticket')
        if ticket['pickup_id'] in pickup_owner \
                and pickup_owner[ticket['pickup_id']] != ticket_id:
            edge(ticket_id, 'pickup-already-allocated', None,
                 against=pickup_owner[ticket['pickup_id']],
                 detail='one-meal-one-voucher')
        blocked = [item for item in conflicts if item['ticket_id'] == ticket_id]
        reasons = sorted({item['kind'] for item in blocked}) or ['no-valid-candidate']
        rows.append({'ticket_id': ticket_id, 'voucher_id': None,
                     'pickup_id': ticket['pickup_id'],
                     'recipient': ticket['recipient'], 'amount': 0,
                     'state': 'unallocated', 'reason': reasons[0],
                     'source': 'unassigned', 'signature_id': None,
                     'image_hash': ticket.get('image', {}).get('image_hash'),
                     'confidence': None, 'ocr_rank': None,
                     'candidate_vouchers': [item['candidate']['voucher_id']
                                            for item in validated[ticket_id]],
                     'blocked_by': reasons})

    rows.sort(key=lambda row: row['ticket_id'])
    payable = sum(row['amount'] for row in rows if row['state'] == 'confirmed')
    unallocated = [row['ticket_id'] for row in rows if row['state'] == 'unallocated']
    evidence_digest = s.digest({'book': book, 'tickets': tickets,
                                'approvals': approvals,
                                'effective_at': int(effective_at)})
    return {'batch_id': evidence.get('batch_id'), 'protocol': protocol,
            'rows': rows, 'conflicts': conflicts, 'unallocated': unallocated,
            'payable_total': payable, 'evidence_digest': evidence_digest,
            'approvals': approvals, 'effective_at': int(effective_at)}


# ---------------------------------------------------------------------------
# Allocation-run persistence and read models.
# ---------------------------------------------------------------------------

def _persist_run(con, batch_id, result, evidence):
    row = con.execute('SELECT COALESCE(MAX(version),0) v FROM ticket_allocation_runs '
                      'WHERE batch_id=?', (batch_id,)).fetchone()
    version = int(row['v']) + 1
    con.execute("UPDATE ticket_allocation_runs SET state='superseded' "
                "WHERE batch_id=? AND state='confirmed'", (batch_id,))
    con.execute("UPDATE ticket_allocations SET state='superseded' "
                "WHERE batch_id=? AND state='confirmed'", (batch_id,))
    state = 'confirmed' if result['payable_total'] > 0 else 'pending-review'
    raw = {'batch_id': batch_id, 'version': version, 'protocol': result['protocol'],
           'approvals': result['approvals'], 'effective_at': result['effective_at'],
           'evidence': evidence, 'result': result}
    con.execute('INSERT INTO ticket_allocation_runs VALUES(?,?,?,?,?,?,?,?)',
                (batch_id, version, result['protocol'], result['evidence_digest'],
                 result['payable_total'], len(result['unallocated']), state,
                 s.canonical(raw)))
    for row in result['rows']:
        allocation_id = f'{batch_id}:v{version}:{row["ticket_id"]}'
        evidence_json = s.canonical({
            'image_hash': row.get('image_hash'),
            'confidence': row.get('confidence'), 'ocr_rank': row.get('ocr_rank'),
            'candidate_vouchers': row.get('candidate_vouchers', []),
            'blocked_by': row.get('blocked_by', [])})
        con.execute('INSERT INTO ticket_allocations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (allocation_id, batch_id, version, row['ticket_id'],
                     row['voucher_id'], row['pickup_id'], row['recipient'],
                     row['amount'], row['state'], row['reason'], row['source'],
                     row['signature_id'], evidence_json))
    return version, state


def assign(con, event):
    data = event.get('data', {})
    batch_id = str(data.get('batch_id', '')).strip()
    evidence = load_evidence(con, batch_id) if batch_id else None
    if not evidence:
        return s.decide(con, event, NAME, 'blocked', 'unknown-ticket-batch', 'blocked')
    result = build_assignment(evidence, data.get('approvals'),
                              effective_at=int(event.get('effective_at', 0)))
    version, state = _persist_run(con, batch_id, result, evidence)
    outcome = 'assigned' if not result['unallocated'] else 'assigned-with-unallocated'
    return s.decide(con, event, NAME, outcome,
                    f'version={version};payable={result["payable_total"]};'
                    f'unallocated={len(result["unallocated"])};'
                    f'protocol={result["protocol"]}', state)


def _run(con, batch_id, version=None):
    if version is None:
        return con.execute('SELECT * FROM ticket_allocation_runs WHERE batch_id=? '
                           'ORDER BY version DESC LIMIT 1', (batch_id,)).fetchone()
    return con.execute('SELECT * FROM ticket_allocation_runs WHERE batch_id=? AND version=?',
                       (batch_id, int(version))).fetchone()


def assignment_table(con, batch_id, version=None):
    run = _run(con, batch_id, version)
    if not run:
        return None
    raw = json.loads(run['raw'])
    return {'batch_id': batch_id, 'version': run['version'],
            'protocol': run['protocol'], 'state': run['state'],
            'payable_total': run['payable_total'],
            'unallocated_count': run['unallocated_count'],
            'evidence_digest': run['evidence_digest'],
            'rows': raw['result']['rows'], 'conflicts': raw['result']['conflicts'],
            'unallocated': raw['result']['unallocated']}


def recompute(con, batch_id, version=None):
    """Re-run the solver from the evidence snapshot stored with the run."""
    run = _run(con, batch_id, version)
    if not run:
        return None
    raw = json.loads(run['raw'])
    result = build_assignment(raw['evidence'], raw.get('approvals'),
                              effective_at=raw.get('effective_at', 0))
    keys = ('ticket_id', 'voucher_id', 'pickup_id', 'recipient', 'amount', 'state')
    table = assignment_table(con, batch_id, run['version'])
    stored = [{key: row.get(key) for key in keys} for row in table['rows']]
    fresh = [{key: row.get(key) for key in keys} for row in result['rows']]
    return {'ok': result['evidence_digest'] == run['evidence_digest'] and stored == fresh,
            'version': run['version'], 'protocol': result['protocol'],
            'evidence_digest': result['evidence_digest'],
            'payable_total': result['payable_total'], 'rows': fresh,
            'conflicts': result['conflicts']}


def confirmed_allocations(con, batch_id, version):
    return [dict(row) for row in con.execute(
        "SELECT * FROM ticket_allocations WHERE batch_id=? AND version=? "
        "AND state='confirmed' ORDER BY ticket_id", (batch_id, int(version)))]


def apply(con, event):
    action = event.get('action')
    if action == 'import_batch':
        return import_batch(con, event)
    if action == 'assign':
        return assign(con, event)
    return s.decide(con, event, NAME, 'blocked', 'unknown-action', 'blocked')

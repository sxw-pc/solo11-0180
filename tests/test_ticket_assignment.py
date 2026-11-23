"""Acceptance tests for offline paper-ticket batch assignment.

The seed baseline in ``test_ticket_seed.py`` proves that the legacy per-image
``claim`` is greedy: two images of one batch can both accept A17.  These tests
prove that the batch path never treats that acceptance as issuance proof, and
that a globally unique assignment is committed only through the solver.
"""
import json
from pathlib import Path

import pytest

from mealassist import cli, settlement, storage, voucher
from mealassist.voucher import claim

FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures' / 'ticket-assignment.json'
BATCH = 'B-2026-11-A'


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def fixture_import(data):
    return {'batch_id': data['batch_id'], 'book': data['book'], 'tickets': data['tickets']}


class Harness:
    def __init__(self, path=':memory:'):
        self.con = storage.connect(path)
        self.n = 0

    def close(self):
        self.con.close()

    def run(self, subsystem, action, subject='batch', **data):
        self.n += 1
        event = {'id': f'e{self.n}', 'subject': subject, 'action': action,
                 'effective_at': self.n, 'received_at': self.n, 'data': data}
        return cli.run_event(self.con, subsystem, event)

    def import_fixture(self, data=None):
        return self.run('voucher', 'import_batch', **fixture_import(data or load_fixture()))

    def assign(self, approvals=None):
        data = {'batch_id': BATCH}
        if approvals is not None:
            data['approvals'] = approvals
        return self.run('voucher', 'assign', **data)

    def table(self, version=None):
        return voucher.assignment_table(self.con, BATCH, version)


@pytest.fixture
def harness():
    instance = Harness()
    yield instance
    instance.close()


def rows_by_ticket(table):
    return {row['ticket_id']: row for row in table['rows']}


def seal(data, signature_id='SIG-T1-A17'):
    """Apply the fixture's sealed-period input to T1's pending signature."""
    sealed = data['sealed_period'][signature_id]
    for ticket in data['tickets']:
        for signature in ticket.get('signatures', []):
            if signature['signature_id'] == signature_id:
                signature.update({'sealed': sealed['sealed'],
                                  'sealed_period': sealed['sealed_period']})
    return data


# ---------------------------------------------------------------------------
# 1. Seed single-ticket baseline and the real greedy collision.
# ---------------------------------------------------------------------------

def test_single_ticket_baseline_is_unchanged():
    ticket = {'ticket_id': 'T1', 'ocr_candidates': [
        {'voucher_id': 'A17', 'amount': 10, 'confidence': 0.9},
        {'voucher_id': 'A71', 'amount': 20, 'confidence': 0.8}]}
    assert claim(ticket) == {'ticket_id': 'T1', 'voucher_id': 'A17',
                             'amount': 10, 'state': 'accepted'}


def test_per_image_claim_reuses_one_voucher():
    """The reproduced defect: greedy claim accepts A17 twice in one batch."""
    t1 = {'ticket_id': 'T1', 'ocr_candidates': [
        {'voucher_id': 'A17', 'amount': 10, 'confidence': 0.9},
        {'voucher_id': 'A71', 'amount': 20, 'confidence': 0.8}]}
    t2 = {'ticket_id': 'T2', 'ocr_candidates': [
        {'voucher_id': 'A17', 'amount': 10, 'confidence': 0.95}]}
    assert claim(t1)['voucher_id'] == 'A17'
    assert claim(t2)['voucher_id'] == 'A17'
    assert claim(t1)['state'] == claim(t2)['state'] == 'accepted'


def test_global_assignment_recovers_the_voucher_greedy_claim_loses(harness):
    data = load_fixture()
    assert harness.import_fixture(data)['outcome'] == 'imported'
    result = harness.assign(data['approvals'])
    assert result['outcome'] == 'assigned'

    table = harness.table()
    assert table['protocol'] == voucher.FULL_REDEMPTION
    assert table['payable_total'] == 30
    assert table['unallocated'] == []
    rows = rows_by_ticket(table)
    assert (rows['T1']['voucher_id'], rows['T1']['amount']) == ('A71', 20)
    assert (rows['T2']['voucher_id'], rows['T2']['amount']) == ('A17', 10)
    assert len({row['voucher_id'] for row in table['rows']}) == 2


# ---------------------------------------------------------------------------
# 2. The two permitted protocols.
# ---------------------------------------------------------------------------

def test_full_redemption_protocol_when_signature_withdrawn(harness):
    data = load_fixture()
    harness.import_fixture(data)
    harness.assign(data['approvals'])
    table = harness.table()
    rows = rows_by_ticket(table)
    assert table['protocol'] == voucher.FULL_REDEMPTION
    assert [row['source'] for row in table['rows']] == ['ocr-global', 'ocr-global']
    assert rows['T1']['voucher_id'] == 'A71'
    assert rows['T2']['voucher_id'] == 'A17'
    assert table['payable_total'] == 30


def test_retain_signature_protocol_without_withdrawal(harness):
    data = load_fixture()
    harness.import_fixture(data)
    result = harness.assign({})  # no approval to withdraw the pending signature
    assert result['outcome'] == 'assigned-with-unallocated'

    table = harness.table()
    rows = rows_by_ticket(table)
    assert table['protocol'] == voucher.RETAIN_SIGNATURE
    assert table['payable_total'] == 10
    assert rows['T1']['state'] == 'confirmed'
    assert rows['T1']['voucher_id'] == 'A17'
    assert rows['T1']['source'] == 'signature'
    assert rows['T2']['state'] == 'unallocated'
    assert rows['T2']['voucher_id'] is None
    assert 'voucher-held-by-signature' in rows['T2']['blocked_by']
    conflict = next(item for item in table['conflicts']
                    if item['ticket_id'] == 'T2' and item['kind'] == 'voucher-held-by-signature')
    assert conflict['voucher_id'] == 'A17'
    assert conflict['against'] == 'T1'


def test_sealed_signature_cannot_be_reoptimised_even_with_approval(harness):
    data = seal(load_fixture())
    harness.import_fixture(data)
    harness.assign(data['approvals'])  # signer + issuer approve, but period is sealed

    table = harness.table()
    rows = rows_by_ticket(table)
    assert table['protocol'] == voucher.RETAIN_SIGNATURE
    assert table['payable_total'] == 10
    assert rows['T1']['voucher_id'] == 'A17'
    assert rows['T1']['reason'] == 'signature-sealed-period'
    assert rows['T2']['state'] == 'unallocated'
    assert 'voucher-held-by-signature' in rows['T2']['blocked_by']


# ---------------------------------------------------------------------------
# 3. Issuance proof, not OCR confidence, and global uniqueness.
# ---------------------------------------------------------------------------

def test_high_confidence_candidate_outside_issued_book_is_rejected(harness):
    data = load_fixture()
    data['tickets'][0]['images'][0]['candidates'] = [
        {'voucher_id': 'A99', 'amount': 10, 'confidence': 0.99, 'chars': 'A99/10'},
        {'voucher_id': 'A71', 'amount': 20, 'confidence': 0.60, 'chars': 'A71/20'}]
    data['tickets'][0]['signatures'] = []
    harness.import_fixture(data)
    harness.assign({})

    table = harness.table()
    rows = rows_by_ticket(table)
    assert rows['T1']['voucher_id'] == 'A71'
    assert rows['T2']['voucher_id'] == 'A17'
    rejected = [item for item in table['conflicts']
                if item['kind'] == 'candidate-rejected']
    if rejected:
        assert any(item['detail'] == 'voucher-not-in-issued-book' for item in rejected)


def test_colliding_reading_is_resolved_by_the_fallback_candidate(harness):
    """A17 is the only reading both images share; it cannot back both tickets.

    T1 can be proved against its second candidate A71, so the global solution
    confirms both: T1->A71 and T2->A17, with no voucher used twice.
    """
    data = load_fixture()
    data['tickets'][0]['signatures'] = []
    harness.import_fixture(data)
    harness.assign({})

    table = harness.table()
    rows = rows_by_ticket(table)
    confirmed = [row for row in table['rows'] if row['state'] == 'confirmed']
    voucher_ids = [row['voucher_id'] for row in confirmed]
    assert len(voucher_ids) == len(set(voucher_ids)) == 2
    assert rows['T1']['voucher_id'] == 'A71'
    assert rows['T2']['voucher_id'] == 'A17'
    assert table['payable_total'] == 30


def test_two_tickets_with_one_shared_voucher_and_no_alternative(harness):
    data = load_fixture()
    data['tickets'][0]['signatures'] = []
    data['tickets'][0]['images'][0]['candidates'] = [
        {'voucher_id': 'A17', 'amount': 10, 'confidence': 0.9, 'chars': 'A17/10'}]
    harness.import_fixture(data)
    harness.assign({})

    table = harness.table()
    confirmed = [row for row in table['rows'] if row['state'] == 'confirmed']
    assert len(confirmed) == 1
    assert confirmed[0]['voucher_id'] == 'A17'
    assert confirmed[0]['ticket_id'] == 'T1'  # deterministic tie: lower ticket id
    loser = rows_by_ticket(table)['T2']
    assert loser['state'] == 'unallocated'
    assert 'voucher-claimed-by-other-ticket' in loser['blocked_by']


def test_one_pickup_is_redeemed_by_at_most_one_voucher(harness):
    data = load_fixture()
    data['tickets'][0]['signatures'] = []
    # Both images are labelled with the same meal and the same senior: two
    # simultaneous paper tickets for one pickup still redeem only once.
    data['tickets'][1]['pickup_id'] = 'P1'
    data['tickets'][1]['recipient'] = 'senior-t1'
    harness.import_fixture(data)
    result = harness.assign({})
    assert result['outcome'] == 'assigned-with-unallocated'

    table = harness.table()
    confirmed = [row for row in table['rows'] if row['state'] == 'confirmed']
    assert len(confirmed) == 1
    loser = next(row for row in table['rows'] if row['state'] == 'unallocated')
    assert 'pickup-already-allocated' in loser['blocked_by']


# ---------------------------------------------------------------------------
# 4. Merchant payable consumes only a confirmed allocation.
# ---------------------------------------------------------------------------

def test_claim_acceptance_alone_creates_no_payable(harness):
    data = load_fixture()
    harness.import_fixture(data)
    result = harness.run('settlement', 'redeem_paper', subject='senior-t1',
                         batch_id=BATCH, version=1, ticket_id='T1')
    assert result['outcome'] == 'blocked'
    assert result['reason'] == 'no-confirmed-allocation'
    assert not harness.con.execute(
        "SELECT 1 FROM receipts WHERE kind='merchant-payable'").fetchone()


def test_payable_consumes_confirmed_allocation_exactly_once(harness):
    data = load_fixture()
    harness.import_fixture(data)
    harness.assign(data['approvals'])

    first = harness.run('settlement', 'redeem_paper', subject='senior-t1',
                        batch_id=BATCH, version=1, ticket_id='T1')
    assert first['outcome'] == 'redeemed'
    assert 'voucher=A71' in first['reason']
    payable = harness.con.execute(
        "SELECT * FROM receipts WHERE kind='merchant-payable'").fetchone()
    assert payable['amount'] == 20

    # Re-using the same allocation, and a posterior unconfirmed version, fail.
    again = harness.run('settlement', 'redeem_paper', subject='senior-t1',
                        batch_id=BATCH, version=1, ticket_id='T1')
    assert again['reason'] == 'allocation-already-redeemed'
    unconfirmed = harness.run('settlement', 'redeem_paper', subject='senior-t1',
                              batch_id=BATCH, version=2, ticket_id='T1')
    assert unconfirmed['reason'] == 'no-confirmed-allocation'
    wrong = harness.run('settlement', 'redeem_paper', subject='senior-t2',
                        batch_id=BATCH, version=1, ticket_id='T1')
    assert wrong['reason'] == 'allocation-recipient-mismatch'


def test_retain_protocol_payable_covers_only_the_retained_voucher(harness):
    data = load_fixture()
    harness.import_fixture(data)
    harness.assign({})

    result = harness.run('settlement', 'redeem_paper', subject='senior-t1',
                         batch_id=BATCH, version=1, ticket_id='T1')
    assert result['outcome'] == 'redeemed'
    assert 'amount=10' in result['reason']
    blocked = harness.run('settlement', 'redeem_paper', subject='senior-t2',
                          batch_id=BATCH, version=1, ticket_id='T2')
    assert blocked['reason'] == 'no-confirmed-allocation'
    assert harness.con.execute(
        "SELECT COALESCE(SUM(amount),0) x FROM receipts WHERE kind='merchant-payable'"
    ).fetchone()['x'] == 10


# ---------------------------------------------------------------------------
# 5. Repeated imports, late clearer images and signature corrections.
# ---------------------------------------------------------------------------

def test_repeated_import_is_idempotent_and_keeps_evidence(harness):
    data = load_fixture()
    assert harness.import_fixture(data)['outcome'] == 'imported'
    images_before = harness.con.execute('SELECT COUNT(*) c FROM ticket_images').fetchone()['c']
    assert harness.import_fixture(data)['outcome'] == 'imported-duplicate'
    assert harness.con.execute('SELECT COUNT(*) c FROM ticket_images').fetchone()['c'] == images_before
    assert harness.con.execute('SELECT COUNT(*) c FROM ticket_signatures').fetchone()['c'] == 1


def test_late_clearer_image_keeps_original_image_and_old_allocation(harness):
    data = load_fixture()
    harness.import_fixture(data)
    harness.assign(data['approvals'])
    first = harness.table()
    first_rows = [(row['ticket_id'], row['voucher_id'], row['state']) for row in first['rows']]

    late = {'batch_id': BATCH, 'book': data['book'], 'tickets': [{
        'ticket_id': 'T2', 'pickup_id': 'P2', 'recipient': 'senior-t2',
        'images': [{'image_hash': 'sha256:t2-clearer', 'received_at': 99,
                    'candidates': [
                        {'voucher_id': 'A17', 'amount': 10, 'confidence': 0.99, 'chars': 'A17/10'},
                        {'voucher_id': 'A71', 'amount': 20, 'confidence': 0.97, 'chars': 'A71/20'}]}]}]}
    assert harness.run('voucher', 'import_batch', **late)['outcome'] == 'imported'

    # The original image hash is still stored alongside the new version.
    hashes = [row['image_hash'] for row in harness.con.execute(
        "SELECT image_hash FROM ticket_images WHERE ticket_id='T2' ORDER BY version")]
    assert hashes == ['sha256:t2-original', 'sha256:t2-clearer']

    harness.assign({})
    new = harness.table()
    old = harness.table(1)
    assert new['version'] == 2
    assert [(row['ticket_id'], row['voucher_id'], row['state']) for row in old['rows']] == first_rows
    assert old['state'] == 'superseded'
    # The pending T1 signature stays pinned to A17; the clearer T2 image can now
    # be proved against A71, so both tickets are honoured without re-optimising
    # the signed voucher.
    assert new['protocol'] == voucher.RETAIN_SIGNATURE
    assert new['payable_total'] == 30
    new_rows = rows_by_ticket(new)
    assert (new_rows['T1']['voucher_id'], new_rows['T1']['source']) == ('A17', 'signature')
    assert (new_rows['T2']['voucher_id'], new_rows['T2']['amount']) == ('A71', 20)
    assert voucher.recompute(harness.con, BATCH)['ok'] is True


def test_signature_correction_appends_version_and_preserves_signed_history(harness):
    data = load_fixture()
    harness.import_fixture(data)
    harness.assign({})
    assert harness.table()['protocol'] == voucher.RETAIN_SIGNATURE
    signed = harness.table()
    assert rows_by_ticket(signed)['T1']['voucher_id'] == 'A17'

    correction = {'batch_id': BATCH, 'tickets': [{
        'ticket_id': 'T1', 'pickup_id': 'P1', 'recipient': 'senior-t1',
        'signatures': [{'signature_id': 'SIG-T1-A17', 'voucher_id': 'A17',
                        'state': 'revoked', 'sealed': False}]}]}
    assert harness.run('voucher', 'import_batch', **correction)['outcome'] == 'imported'
    versions = [row['version'] for row in harness.con.execute(
        "SELECT version FROM ticket_signatures WHERE signature_id='SIG-T1-A17' ORDER BY version")]
    assert versions == [1, 2]

    harness.assign({})
    corrected = harness.table()
    assert corrected['version'] == 2
    assert corrected['protocol'] == voucher.FULL_REDEMPTION
    assert rows_by_ticket(corrected)['T1']['voucher_id'] == 'A71'
    assert rows_by_ticket(corrected)['T2']['voucher_id'] == 'A17'
    # The original signed allocation version and event evidence survive.
    original = harness.table(1)
    assert rows_by_ticket(original)['T1']['voucher_id'] == 'A17'
    assert original['protocol'] == voucher.RETAIN_SIGNATURE


def test_tampered_evidence_is_detected_on_replay(harness):
    data = load_fixture()
    harness.import_fixture(data)
    harness.assign(data['approvals'])
    assert cli.audit(harness.con)['ok'] is True
    harness.con.execute("UPDATE decisions SET outcome='tampered' WHERE event_id='e2'")
    harness.con.commit()
    assert cli.audit(harness.con)['ok'] is False


def test_database_refuses_second_confirmed_voucher_allocation(harness):
    data = load_fixture()
    harness.import_fixture(data)
    harness.assign(data['approvals'])
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        harness.con.execute(
            'INSERT INTO ticket_allocations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('bogus', BATCH, 1, 'T3', 'A17', 'P3', 'senior-t3', 10, 'confirmed',
             'manual', 'manual', None, '{}'))

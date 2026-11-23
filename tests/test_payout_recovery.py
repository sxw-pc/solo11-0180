import json, tempfile, unittest
from pathlib import Path
from mealassist import cli, payout, storage, bank
from mealassist.payout import pay, recover, reverse, PayoutError


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / 'local.sqlite'
        self.bank = self.root / 'bank.json'
        self.con = storage.connect(self.db)
        self.n = 0

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def go(self, sub, action, subject='senior-1', **data):
        self.n += 1
        return cli.run_event(self.con, sub, {'id': f'e{self.n}', 'subject': subject,
                                             'action': action, 'data': data,
                                             'effective_at': self.n, 'received_at': self.n})

    def payable(self, pid='p1', amount=15, subject='senior-1'):
        with self.con:
            self.con.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?,?)',
                             (pid, subject, 'merchant-payable', 'open', amount))

    def state(self, rid, con=None):
        con = con or self.con
        return storage.latest(con, 'receipts', rid)['state']

    def bank_payments(self, key=None, path=None):
        rows = [r for r in bank.rows(path or self.bank)
                if r.get('kind', 'payment') == 'payment' and r.get('status') != 'conflict']
        return [r for r in rows if key is None or r['idempotency_key'] == key]

    def payouts(self, con=None):
        con = con or self.con
        return con.execute("SELECT * FROM receipts WHERE kind='merchant-payout'").fetchall()


class RecoveryAfterBank(Base):
    def test_seed_breakpoint(self):
        """The seeded failure: bank moved 15, local has no payout, payable open."""
        self.payable()
        with self.assertRaisesRegex(RuntimeError, 'connection_lost_after_bank'):
            pay(self.db, self.bank, 'PAY1', 'shop-A', 'p1', fail_after_bank=True)
        rows = json.loads(self.bank.read_text())
        self.assertEqual([(r['payment_id'], r['amount'], r['status']) for r in rows],
                         [('PAY1', 15, 'transferred')])
        self.assertEqual(len(self.payouts()), 0)
        self.assertEqual(self.state('p1'), 'open')
        intent = storage.intent_for(self.con, 'p1')
        self.assertEqual(intent['state'], 'pending')
        self.assertEqual(intent['bank_key'], 'p1')

    def test_retry_with_pay2_does_not_transfer_twice(self):
        self.payable()
        with self.assertRaisesRegex(RuntimeError, 'connection_lost_after_bank'):
            pay(self.db, self.bank, 'PAY1', 'shop-A', 'p1', fail_after_bank=True)
        # A queue that only looks at open payables runs pay() again with PAY2.
        result = pay(self.db, self.bank, 'PAY2', 'shop-A', 'p1')
        self.assertEqual(result['state'], 'settled')
        self.assertEqual(result['bank_receipt'], 'payment:p1:PAY1')
        payments = self.bank_payments('p1')
        self.assertEqual(len(payments), 1, 'PAY1 must be the only money movement')
        self.assertEqual(payments[0]['payment_id'], 'PAY1')
        self.assertEqual(sum(r['amount'] for r in payments), 15)
        self.assertEqual(self.state('p1'), 'settled')
        self.assertEqual(len(self.payouts()), 1)
        self.assertEqual(self.payouts()[0]['id'], 'PAY1')
        self.assertEqual(self.payouts()[0]['amount'], 15)
        self.assertEqual(self.state('PAY1'), 'paid')
        # The intent is the single link between payable, bank and confirmation.
        intent = storage.intent_for(self.con, 'p1')
        self.assertEqual(intent['state'], 'settled')
        self.assertEqual(intent['receipt_id'], 'PAY1')
        self.assertEqual(intent['amount'], 15)

    def test_retry_same_pay1_is_idempotent(self):
        self.payable()
        pay(self.db, self.bank, 'PAY1', 'shop-A', 'p1')
        before = self.bank.read_text()
        result = pay(self.db, self.bank, 'PAY1', 'shop-A', 'p1')
        self.assertEqual(result['state'], 'already')
        self.assertEqual(self.bank.read_text(), before)
        self.assertEqual(len(self.bank_payments('p1')), 1)

    def test_recover_after_breakpoint(self):
        self.payable()
        with self.assertRaisesRegex(RuntimeError, 'connection_lost_after_bank'):
            pay(self.db, self.bank, 'PAY1', 'shop-A', 'p1', fail_after_bank=True)
        results = recover(self.db, self.bank)
        self.assertEqual(results[0]['state'], 'settled')
        self.assertEqual(self.state('p1'), 'settled')
        self.assertEqual(len(self.payouts()), 1)
        # Replay of recovery produces nothing new.
        self.assertEqual(recover(self.db, self.bank), [])
        self.assertEqual(len(self.payouts()), 1)
        self.assertEqual(len(self.bank_payments('p1')), 1)

    def test_pay_after_recover_is_already(self):
        self.payable()
        with self.assertRaisesRegex(RuntimeError, 'connection_lost_after_bank'):
            pay(self.db, self.bank, 'PAY1', 'shop-A', 'p1', fail_after_bank=True)
        recover(self.db, self.bank)
        self.assertEqual(pay(self.db, self.bank, 'PAY2', 'shop-A', 'p1')['state'], 'already')
        self.assertEqual(len(self.bank_payments('p1')), 1)

    def test_bank_receipt_belongs_to_one_payable(self):
        self.payable('p1')
        self.payable('p2')
        first = pay(self.db, self.bank, 'PAY1', 'shop-A', 'p1')
        self.assertEqual(first['state'], 'settled')
        # p2 uses p1's bank receipt id through a manipulated external row: the
        # unique index on external_payments.receipt_id prevents reattachment.
        with self.assertRaises(Exception):
            with self.con:
                self.con.execute(
                    'INSERT INTO external_payments(receipt_id,payable_id,intent_id,kind,idempotency_key,payment_id,merchant,payee,amount,status,raw) '
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (first['bank_receipt'], 'p2', 'intent:p2', 'payment', 'p2', 'PAY9', 'shop-A', 'shop-A', 15, 'transferred', '{}'))
        self.assertEqual(self.con.execute(
            "SELECT payable_id FROM external_payments WHERE receipt_id=?", (first['bank_receipt'],)).fetchone()[0], 'p1')

    def test_amount_or_merchant_mismatch_freezes_and_keeps_evidence(self):
        self.payable('p1', amount=15)
        with self.con:
            # A bank receipt from an earlier run that disagrees with the payable.
            self.con.execute(
                'INSERT INTO external_payments(receipt_id,payable_id,intent_id,kind,idempotency_key,payment_id,merchant,payee,amount,status,raw) '
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ('legacy:p1', 'p1', 'intent:p1', 'payment', 'p1', 'PAY0', 'shop-A', 'shop-A', 12, 'transferred', '{"original":true}'))
        result = pay(self.db, self.bank, 'PAY2', 'shop-A', 'p1')
        self.assertEqual(result['state'], 'frozen')
        self.assertEqual(self.state('p1'), 'frozen')
        self.assertEqual(self.payouts(), [])
        # Nothing new was sent to the bank, and the old evidence survives.
        self.assertEqual(self.bank_payments('p1'), [])
        kept = self.con.execute("SELECT raw FROM external_payments WHERE receipt_id='legacy:p1'").fetchone()
        self.assertEqual(json.loads(kept['raw']), {'original': True})

    def test_merchant_mismatch_blocks_before_bank(self):
        self.payable()
        with self.con:
            self.con.execute('INSERT INTO documents VALUES(?,?,?,?,?)',
                             ('p1:terms', 'senior-1', 'payable-terms', 1,
                              json.dumps({'merchant': 'shop-A', 'payee': 'shop-A', 'amount': 15,
                                          'funding_source': 'city_subsidy',
                                          'budget_account': 'city-subsidy-account',
                                          'budget_amount': 15, 'recourse': 'merchant',
                                          'merchant_known': True})))
        with self.assertRaises(PayoutError) as ctx:
            pay(self.db, self.bank, 'PAY2', 'shop-B', 'p1')
        self.assertEqual(ctx.exception.code, 'payable-merchant-mismatch')
        self.assertEqual(self.bank_payments(), [])
        self.assertEqual(self.state('p1'), 'open')

    def test_funding_source_mismatch_blocks_before_bank(self):
        # A payable recorded for the city path cannot be paid as a senior
        # reimbursement.
        self.payable('p9')
        with self.con:
            self.con.execute('INSERT INTO documents VALUES(?,?,?,?,?)',
                             ('p9:terms', 'senior-1', 'payable-terms', 1,
                              json.dumps({'merchant': 'shop-A', 'payee': 'shop-A', 'amount': 15,
                                          'funding_source': 'city_subsidy',
                                          'budget_account': 'city-subsidy-account',
                                          'budget_amount': 15, 'recourse': 'merchant',
                                          'merchant_known': True})))
        with self.assertRaises(PayoutError) as ctx:
            pay(self.db, self.bank, 'PAY2', 'shop-A', 'p9', funding_source=payout.SENIOR)
        self.assertEqual(ctx.exception.code, 'payable-funding-source-mismatch')
        self.assertEqual(self.bank_payments(), [])
        self.assertEqual(self.state('p9'), 'open')


class BankOutcomes(Base):
    def test_unknown_result_freezes_and_blocks_second_transfer(self):
        self.payable()
        first = pay(self.db, self.bank, 'PAY1', 'shop-A', 'p1', bank_status='unknown')
        self.assertEqual(first['state'], 'unknown')
        second = pay(self.db, self.bank, 'PAY2', 'shop-A', 'p1')
        self.assertEqual(second['state'], 'unknown')
        self.assertEqual(len(self.bank_payments('p1')), 1)
        self.assertEqual(sum(r['amount'] for r in self.bank_payments('p1')), 15)
        self.assertEqual(self.payouts(), [])
        self.assertNotEqual(self.state('p1'), 'settled')

    def test_unknown_then_transferred_settles_on_recovery(self):
        self.payable()
        pay(self.db, self.bank, 'PAY1', 'shop-A', 'p1', bank_status='unknown')
        bank.set_status(self.bank, 'p1', 'transferred')
        results = recover(self.db, self.bank)
        self.assertEqual(results[0]['state'], 'settled')
        self.assertEqual(self.state('p1'), 'settled')
        self.assertEqual(len(self.bank_payments('p1')), 1)

    def test_unknown_evidence_survives_lost_bank_file(self):
        self.payable()
        pay(self.db, self.bank, 'PAY1', 'shop-A', 'p1', bank_status='unknown')
        self.bank.unlink()  # bank file lost; the mirrored receipt must still stop a retry
        result = pay(self.db, self.bank, 'PAY2', 'shop-A', 'p1')
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(bank.rows(self.bank), [])
        self.assertNotEqual(self.state('p1'), 'settled')
        self.assertEqual(self.payouts(), [])

    def test_accepted_waits_for_final_result(self):
        self.payable()
        self.assertEqual(pay(self.db, self.bank, 'PAY1', 'shop-A', 'p1', bank_status='accepted')['state'], 'pending')
        self.assertEqual(len(self.bank_payments('p1')), 1)
        pay(self.db, self.bank, 'PAY2', 'shop-A', 'p1')  # must not resubmit
        self.assertEqual(len(self.bank_payments('p1')), 1)
        bank.set_status(self.bank, 'p1', 'transferred')
        self.assertEqual(recover(self.db, self.bank)[0]['state'], 'settled')
        self.assertEqual(self.state('p1'), 'settled')

    def test_rejected_unfreezes_and_allows_retry_under_same_key(self):
        self.payable()
        self.assertEqual(pay(self.db, self.bank, 'PAY1', 'shop-A', 'p1', bank_status='rejected')['state'], 'rejected')
        self.assertEqual(self.state('p1'), 'open')
        # Bank's final word is now accepted/transferred; retry adopts it.
        bank.set_status(self.bank, 'p1', 'transferred')
        result = pay(self.db, self.bank, 'PAY2', 'shop-A', 'p1')
        self.assertEqual(result['state'], 'settled')
        self.assertEqual(len(self.bank_payments('p1')), 1)

    def test_conflict_freezes_and_preserves_both_receipts(self):
        self.payable()
        pay(self.db, self.bank, 'PAY1', 'shop-A', 'p1', bank_status='unknown')
        # A second submission with a different payee/amount is recorded as a
        # conflicting receipt, but the original is not touched.
        disputed = bank.transfer(self.bank, {'idempotency_key': 'p1', 'payment_id': 'PAY2',
                                             'merchant': 'shop-B', 'payee': 'shop-B', 'amount': 30,
                                             'payable_id': 'p1'})
        self.assertEqual(disputed['status'], 'conflict')
        rows = bank.rows(self.bank)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['amount'], 15)
        self.assertEqual(rows[0]['status'], 'unknown')
        result = recover(self.db, self.bank)
        self.assertEqual(result[0]['state'], 'frozen')
        self.assertEqual(self.state('p1'), 'frozen')
        self.assertEqual(len(bank.rows(self.bank)), 2, 'no bank fact may be deleted')


class Reversal(Base):
    def test_reversal_is_a_new_auditable_transaction(self):
        self.payable()
        pay(self.db, self.bank, 'PAY1', 'shop-A', 'p1')
        before = bank.rows(self.bank)
        result = reverse(self.db, self.bank, 'REV1', 'p1', reason='eligibility-revoked')
        self.assertEqual(result['state'], 'reversed')
        rows = bank.rows(self.bank)
        self.assertEqual(rows[:len(before)], before, 'original bank facts stay untouched')
        reversal = rows[-1]
        self.assertEqual(reversal['kind'], 'reversal')
        self.assertEqual(reversal['reversal_of'], 'payment:p1:PAY1')
        self.assertEqual(reversal['amount'], -15)
        # Local ledger keeps both postings; net budget is zero.
        self.assertEqual(self.state('p1'), 'reversed')
        self.assertEqual(self.state('PAY1'), 'reversed')
        self.assertEqual(self.state('REV1'), 'reversed')
        postings = self.con.execute("SELECT kind,amount FROM budget_entries WHERE payable_id='p1' ORDER BY id").fetchall()
        self.assertEqual([(p['kind'], p['amount']) for p in postings],
                         [('subsidy-spend', 15), ('subsidy-spend-reversal', -15)])
        self.assertEqual(sum(p['amount'] for p in postings), 0)
        # Reversing an already reversed payout is refused; there is exactly
        # one reversal and the original bank facts still stand.
        with self.assertRaises(PayoutError) as ctx:
            reverse(self.db, self.bank, 'REV1', 'p1')
        self.assertEqual(ctx.exception.code, 'no-settled-payout')
        self.assertEqual(len([r for r in bank.rows(self.bank) if r['kind'] == 'reversal']), 1)

    def test_reversal_requires_settled_payout(self):
        self.payable()
        with self.assertRaises(PayoutError) as ctx:
            reverse(self.db, self.bank, 'REV1', 'p1')
        self.assertEqual(ctx.exception.code, 'no-settled-payout')
        self.assertEqual(bank.rows(self.bank), [])


class FundingPaths(Base):
    def _meal(self, funding_source, **data):
        self.go('eligibility', 'enroll', monthly_limit=30)
        quota = self.go('eligibility', 'reserve', amount=10)
        prep = self.go('service', 'prepare', meal='lunch', stock=2, orders=1)
        pickup = self.go('service', 'pickup', prepare=prep['event'])
        if funding_source == payout.SENIOR:
            proof = self.go('settlement', 'senior_payment', merchant='shop-A', amount=15)
            self.assertEqual(proof['outcome'], 'senior-paid')
            data['payment_proof'] = proof['event']
        redeem = self.go('settlement', 'redeem', pickup=pickup['event'], reserve=quota['event'],
                         price=15, merchant='shop-A', funding_source=funding_source, **data)
        return redeem

    def test_city_subsidy_pays_merchant_in_full(self):
        redeem = self._meal(payout.CITY)
        self.assertEqual(redeem['outcome'], 'redeemed')
        pay(self.db, self.bank, 'PAY1', 'shop-A', redeem['event'])
        result = pay(self.db, self.bank, 'PAY1', 'shop-A', redeem['event'])
        self.assertEqual(result['state'], 'already')
        external = storage.external_for(self.con, redeem['event'])
        self.assertEqual((external['payee'], external['amount']), ('shop-A', 15))
        budget = self.con.execute("SELECT account,funding_source,amount FROM budget_entries WHERE payable_id=?",
                                  (redeem['event'],)).fetchone()
        self.assertEqual(tuple(budget), ('city-subsidy-account', payout.CITY, 15))
        terms = json.loads(self.con.execute(
            "SELECT state FROM documents WHERE id=?", (redeem['event'] + ':terms',)).fetchone()['state'])
        self.assertEqual(terms['recourse'], 'merchant')

    def test_senior_reimbursement_pays_senior_only_subsidy(self):
        redeem = self._meal(payout.SENIOR)
        self.assertEqual(redeem['outcome'], 'redeemed')
        pay(self.db, self.bank, 'PAY1', 'shop-A', redeem['event'])
        external = storage.external_for(self.con, redeem['event'])
        # The merchant was already paid by the senior; no merchant transfer.
        self.assertEqual((external['payee'], external['amount']), ('senior-1', 10))
        self.assertEqual(external['merchant'], 'shop-A')
        budget = self.con.execute("SELECT account,funding_source,amount FROM budget_entries WHERE payable_id=?",
                                  (redeem['event'],)).fetchone()
        self.assertEqual(tuple(budget), ('senior-reimbursement-account', payout.SENIOR, 10))
        self.assertEqual(self.payouts(), [])
        self.assertEqual(len([r for r in bank.rows(self.bank) if r['payee'] == 'shop-A']), 0)
        terms = json.loads(self.con.execute(
            "SELECT state FROM documents WHERE id=?", (redeem['event'] + ':terms',)).fetchone()['state'])
        self.assertEqual(terms['recourse'], 'senior')

    def test_both_paths_share_inputs_but_differ_in_accounting(self):
        city = self._meal(payout.CITY)
        city_terms = json.loads(self.con.execute(
            "SELECT state FROM documents WHERE id=?", (city['event'] + ':terms',)).fetchone()['state'])
        senior = self._meal(payout.SENIOR)
        senior_terms = json.loads(self.con.execute(
            "SELECT state FROM documents WHERE id=?", (senior['event'] + ':terms',)).fetchone()['state'])
        for key in ('merchant', 'price', 'payable_id'):
            if key != 'payable_id':
                self.assertEqual(city_terms[key], senior_terms[key])
        self.assertEqual(city_terms['amount'] + senior_terms['amount'], 25)  # 15 merchant + 10 senior
        self.assertNotEqual(city_terms['payee'], senior_terms['payee'])
        self.assertNotEqual(city_terms['budget_account'], senior_terms['budget_account'])
        self.assertNotEqual(city_terms['recourse'], senior_terms['recourse'])

    def test_senior_path_requires_payment_proof(self):
        self.go('eligibility', 'enroll', monthly_limit=30)
        quota = self.go('eligibility', 'reserve', amount=10)
        prep = self.go('service', 'prepare', meal='lunch', stock=2, orders=1)
        pickup = self.go('service', 'pickup', prepare=prep['event'])
        blocked = self.go('settlement', 'redeem', pickup=pickup['event'], reserve=quota['event'],
                          price=15, merchant='shop-A', funding_source=payout.SENIOR)
        self.assertEqual(blocked['outcome'], 'blocked')
        self.assertEqual(blocked['reason'], 'senior-payment-proof-invalid')

    def test_payment_proof_cannot_fund_two_payables(self):
        first = self._meal(payout.SENIOR)
        self.assertEqual(first['outcome'], 'redeemed')
        proof = self.con.execute("SELECT state FROM documents WHERE kind='reimbursement-proof'").fetchone()['state']
        # A second redemption with the same proof is refused.
        quota = self.go('eligibility', 'reserve', amount=10)
        prep = self.go('service', 'prepare', meal='lunch', stock=2, orders=1)
        pickup = self.go('service', 'pickup', prepare=prep['event'])
        second = self.go('settlement', 'redeem', pickup=pickup['event'], reserve=quota['event'],
                         price=15, merchant='shop-A', funding_source=payout.SENIOR,
                         payment_proof=proof)
        self.assertEqual(second['outcome'], 'blocked')
        self.assertEqual(second['reason'], 'senior-payment-proof-already-used')

    def test_reconcile_uses_bank_receipt_not_claimed_amount(self):
        redeem = self._meal(payout.CITY)
        pay(self.db, self.bank, 'PAY1', 'shop-A', redeem['event'])
        # Caller claims 15 but the bank row says the payee/merchant disagree:
        # reconciliation reads the stored receipt instead of trusting ``received``.
        with self.con:
            self.con.execute("UPDATE external_payments SET amount=12 WHERE payable_id=?", (redeem['event'],))
        decision = self.go('settlement', 'reconcile', payable=redeem['event'], received=15)
        self.assertEqual(decision['outcome'], 'frozen')

    def test_trace_is_fully_auditable(self):
        redeem = self._meal(payout.CITY)
        with self.assertRaisesRegex(RuntimeError, 'connection_lost_after_bank'):
            pay(self.db, self.bank, 'PAY1', 'shop-A', redeem['event'], fail_after_bank=True)
        pay(self.db, self.bank, 'PAY2', 'shop-A', redeem['event'])
        trace = payout.trace(self.db, redeem['event'])
        self.assertEqual(trace['intent']['amount'], 15)
        self.assertEqual([e['payment_id'] for e in trace['external']], ['PAY1'])
        self.assertEqual([a['action'] for a in trace['attempts']], ['posted'])
        self.assertEqual(len(trace['budget']), 1)
        self.assertEqual(len(bank.rows(self.bank)), 1)
        # The trace carries the payable, the paid order and the bank receipt,
        # not the senior's unrelated quota/pickup receipts.
        self.assertEqual({r['id'] for r in trace['receipts']},
                         {redeem['event'], 'PAY1'})


class FixtureScenario(Base):
    def test_audit_survives_payouts_but_still_catches_tampering(self):
        self.payable('p-a', subject='senior-1')
        pay(self.db, self.bank, 'PAY-A', 'shop-A', 'p-a')
        # A payout confirmation is external, so it is excluded from event
        # replay; the rest of the ledger must still replay cleanly.
        self.assertTrue(cli.audit(self.con)['ok'])
        self.go('eligibility', 'enroll', monthly_limit=30)
        self.assertTrue(cli.audit(self.con)['ok'])
        with self.con:
            self.con.execute("UPDATE decisions SET outcome='tampered' WHERE event_id='e1'")
        result = cli.audit(self.con)
        self.assertFalse(result['ok'])
        self.assertEqual(result['mismatches'], ['decisions'])

    def test_fixture_matches_codepath(self):
        fixture = json.loads((Path(__file__).resolve().parent.parent / 'fixtures' / 'payout-recovery.json').read_text())
        self.payable(fixture['payable']['id'], fixture['payable']['amount'], fixture['payable']['subject'])
        with self.assertRaisesRegex(RuntimeError, fixture['steps'][0]['expect']['raises']):
            pay(self.db, self.bank, 'PAY1', fixture['merchant'], 'p1', fail_after_bank=True)
        self.assertEqual(self.bank_payments('p1')[0]['status'], 'transferred')
        self.assertEqual(len(self.payouts()), fixture['steps'][0]['expect']['local_payouts'])
        pay(self.db, self.bank, 'PAY2', fixture['merchant'], 'p1')
        expect = fixture['steps'][1]['expect']
        self.assertEqual(len(self.bank_payments('p1')), expect['bank_payments_for_key'])
        self.assertEqual(sum(r['amount'] for r in self.bank_payments('p1')), expect['bank_total'])
        self.assertEqual(len(self.payouts()), expect['local_payouts'])
        results = recover(self.db, self.bank)
        replayed = fixture['steps'][2]['expect']
        self.assertEqual(len(results), 0)
        self.assertEqual(len(self.bank_payments('p1')), replayed['bank_payments_for_key'])
        self.assertEqual(len(self.payouts()), replayed['local_payouts'])

    def test_fixture_unknown_and_conflict_steps(self):
        fixture = json.loads((Path(__file__).resolve().parent.parent / 'fixtures' / 'payout-recovery.json').read_text())
        self.payable('p2', amount=15)
        unknown_expect = fixture['steps'][3]['expect']
        result = pay(self.db, self.bank, 'PAY3', 'shop-B', 'p2', bank_status='unknown')
        self.assertEqual(result['state'], unknown_expect['state'])
        self.assertEqual(pay(self.db, self.bank, 'PAY4', 'shop-B', 'p2')['state'], unknown_expect['second_pay_state'])
        self.assertEqual(len(self.bank_payments('p2')), unknown_expect['bank_payments_for_key'])
        self.assertEqual(self.state('p2'), unknown_expect['payable_state'])

        bank2 = self.root / 'bank2.json'
        self.payable('p3', amount=15)
        conflict_expect = fixture['steps'][4]['expect']
        result = pay(self.db, bank2, 'PAY4', 'shop-C', 'p3', bank_status='conflict')
        self.assertEqual(result['state'], conflict_expect['state'])
        self.assertEqual(self.state('p3'), conflict_expect['payable_state'])
        self.assertEqual(len(bank.rows(bank2)), conflict_expect['bank_rows_preserved'])

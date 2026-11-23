"""Concurrency and replay acceptance checks for the payout flow."""
import json, tempfile, threading, unittest
from pathlib import Path
from mealassist import cli, payout, storage, bank


class Concurrency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / 'local.sqlite'
        self.bank = self.root / 'bank.json'
        con = storage.connect(self.db)
        with con:
            con.execute('INSERT INTO receipts VALUES(?,?,?,?,?)',
                        ('p1', 'senior-1', 'merchant-payable', 'open', 15))
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _race(self, n=6):
        results, errors = [], []
        barrier = threading.Barrier(n)

        def worker(i):
            try:
                barrier.wait()
                results.append(payout.pay(self.db, self.bank, f'PAY{i}', 'shop-A', 'p1'))
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads: t.start()
        for t in threads: t.join()
        return results, errors

    def test_concurrent_pay_yields_one_transfer_and_one_confirmation(self):
        results, errors = self._race()
        self.assertEqual(errors, [])
        payments = [r for r in bank.rows(self.bank)
                    if r.get('kind', 'payment') == 'payment' and r['status'] != 'conflict']
        self.assertEqual(len(payments), 1)
        self.assertEqual(sum(r['amount'] for r in payments), 15)
        con = storage.connect(self.db)
        try:
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM receipts WHERE kind IN ('merchant-payout','senior-reimbursement')"
            ).fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM budget_entries").fetchone()[0], 1)
            self.assertEqual(storage.latest(con, 'receipts', 'p1')['state'], 'settled')
        finally:
            con.close()

    def test_concurrent_recover_is_idempotent(self):
        with self.assertRaises(RuntimeError):
            payout.pay(self.db, self.bank, 'PAY1', 'shop-A', 'p1', fail_after_bank=True)
        errors, results = [], []
        barrier = threading.Barrier(5)

        def worker():
            try:
                barrier.wait()
                results.extend(payout.recover(self.db, self.bank))
            except Exception as exc:  # pragma: no cover
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors, [])
        con = storage.connect(self.db)
        try:
            self.assertEqual(con.execute(
                "SELECT COUNT(*) FROM receipts WHERE kind='merchant-payout'").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM budget_entries").fetchone()[0], 1)
        finally:
            con.close()
        self.assertEqual(len(bank.rows(self.bank)), 1)

    def test_replayed_event_does_not_move_money_twice(self):
        con = storage.connect(self.db)
        try:
            event = {'id': 'intent-ev', 'subject': 'senior-1', 'action': 'payout-intent',
                     'data': {'payable': 'p1', 'merchant': 'shop-A'}, 'effective_at': 1, 'received_at': 1}
            first = cli.run_event(con, 'settlement', event)
            second = cli.run_event(con, 'settlement', event)
        finally:
            con.close()
        self.assertEqual(first['outcome'], 'blocked')  # unknown action, but recorded once
        self.assertEqual(second['outcome'], 'idempotent')
        self.assertEqual(bank.rows(self.bank), [])

    def test_replayed_pay_of_settled_payable_is_already(self):
        payout.pay(self.db, self.bank, 'PAY1', 'shop-A', 'p1')
        before = self.bank.read_text()
        for i in range(3):
            self.assertEqual(payout.pay(self.db, self.bank, 'PAY1', 'shop-A', 'p1')['state'], 'already')
        self.assertEqual(self.bank.read_text(), before)
        self.assertEqual(len(bank.rows(self.bank)), 1)

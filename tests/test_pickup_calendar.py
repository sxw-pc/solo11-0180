"""Acceptance tests for the ICS pickup-calendar ledger.

Covers: UTC no-switch baseline (kept in tests/test_calendar_seed.py),
Berlin second-week 07:00Z/08:00Z for the two attested time protocols,
EXDATE + single-reschedule unique resolution, fold/gap wall clocks kept
unresolved, cancel-before-series ordering, idempotent re-import, offline
resume, cross-institution UID isolation and no double meal preparation.
"""
import json
import unittest
from pathlib import Path

from mealassist import cli, storage
from mealassist.calendar_feed import expand_series, parse_ics
from mealassist.visit_capture import ingest_ics, instances, materialize, windows

FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures' / 'pickup-calendar.ics'
ORG = 'org-a'
BREAKFAST = 'B1-breakfast@haixin-care.example'
MEDICATION = 'B1-medication@haixin-care.example'
UNATTESTED = 'B1-unattested@haixin-care.example'
AUTUMN = 'B1-autumn@haixin-care.example'
GAP = 'B1-gap@haixin-care.example'

CANCEL_BREAKFAST_WK3 = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//mealassist//pickup-update//ZH
METHOD:CANCEL
BEGIN:VEVENT
UID:B1-breakfast@haixin-care.example
RECURRENCE-ID;TZID=Europe/Berlin:20260405T090000
DTSTAMP:20260401T080000Z
SEQUENCE:5
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""

# A local-wall-clock exception aimed at the UTC-interval medication series:
# it must NOT cancel the 08:00Z instance without authorization.
LOCAL_CANCEL_MEDICATION = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//mealassist//pickup-update//ZH
METHOD:CANCEL
BEGIN:VEVENT
UID:B1-medication@haixin-care.example
RECURRENCE-ID;TZID=Europe/Berlin:20260329T090000
DTSTAMP:20260401T080000Z
SEQUENCE:6
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""

# The authorized way to cancel the medication series' second instance:
# a UTC-form RECURRENCE-ID matching the series' own recurrence key domain.
UTC_CANCEL_MEDICATION = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//mealassist//pickup-update//ZH
METHOD:CANCEL
BEGIN:VEVENT
UID:B1-medication@haixin-care.example
RECURRENCE-ID:20260329T080000Z
DTSTAMP:20260402T080000Z
SEQUENCE:7
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR
"""


class PickupCalendar(unittest.TestCase):
    def setUp(self):
        self.con = storage.connect(':memory:')
        self.ics = FIXTURE.read_text(encoding='utf-8')

    def tearDown(self):
        self.con.close()

    def by_uid(self, uid):
        return sorted((r for r in instances(self.con) if r['uid'] == uid),
                      key=lambda r: (r['seq'], r['id']))

    def base_event(self, uid):
        return [e for e in parse_ics(self.ics)['events']
                if e['uid'] == uid and e['recurrence_id'] is None][0]

    # --- the two attested time protocols on the same first B1 day ----------

    def test_wallclock_second_week_is_0700z(self):
        ev = self.base_event(BREAKFAST)
        occs = expand_series(ev['dtstart'], ev['rrule'], ev['time_mode'], ev['display_tz'])
        self.assertEqual([o['local'] for o in occs],
                         ['2026-03-22T09:00:00', '2026-03-29T09:00:00', '2026-04-05T09:00:00'])
        self.assertEqual([o['utc'] for o in occs],
                         ['2026-03-22T08:00:00+00:00', '2026-03-29T07:00:00+00:00',
                          '2026-04-05T07:00:00+00:00'])

    def test_utc_interval_second_week_is_0800z(self):
        ev = self.base_event(MEDICATION)
        occs = expand_series(ev['dtstart'], ev['rrule'], ev['time_mode'], ev['display_tz'])
        self.assertEqual([o['utc'] for o in occs],
                         ['2026-03-22T08:00:00+00:00', '2026-03-29T08:00:00+00:00',
                          '2026-04-05T08:00:00+00:00'])
        self.assertEqual(occs[1]['local'], '2026-03-29T10:00:00')
        self.assertEqual(occs[1]['tzid'], 'Europe/Berlin')

    # --- EXDATE + single reschedule resolve to exactly one instance ---------

    def test_exdate_and_override_resolve_to_single_instance(self):
        ingest_ics(self.con, self.ics, ORG)
        rows = self.by_uid(BREAKFAST)
        self.assertEqual(len(rows), 3)
        wk1, wk2, wk3 = rows
        self.assertEqual((wk1['local'], wk1['utc'], wk1['status'], wk1['fold']),
                         ('2026-03-22T09:00:00', '2026-03-22T08:00:00+00:00', 'confirmed', 0))
        self.assertEqual(wk1['window_end'], '2026-03-22T08:30:00+00:00')
        self.assertEqual(wk1['attest_version'], 3)
        # the local 09:00 key is uniquely located: no leftover 09:00 instance,
        # exactly one 11:00 replacement (09:00Z) for the same meal
        self.assertEqual(wk2['rkey'], 'local|Europe/Berlin|2026-03-29T09:00:00')
        self.assertEqual((wk2['local'], wk2['utc'], wk2['status']),
                         ('2026-03-29T11:00:00', '2026-03-29T09:00:00+00:00', 'confirmed'))
        self.assertEqual(wk2['rescheduled'], 1)
        self.assertEqual(wk2['attest_version'], 4)
        self.assertEqual((wk3['local'], wk3['utc'], wk3['status']),
                         ('2026-04-05T09:00:00', '2026-04-05T07:00:00+00:00', 'confirmed'))
        self.assertEqual([r for r in rows if r['local'] == '2026-03-29T09:00:00'], [])
        self.assertEqual(len([r for r in instances(self.con)
                              if r['utc'] == '2026-03-29T09:00:00+00:00']), 1)
        self.assertEqual(len([w for w in windows(self.con) if w['uid'] == BREAKFAST]), 3)

    # --- the local exception cannot cross onto the UTC-interval series ------

    def test_local_exception_cannot_cross_to_utc_interval_series(self):
        ingest_ics(self.con, self.ics, ORG)
        res = ingest_ics(self.con, LOCAL_CANCEL_MEDICATION, ORG)
        self.assertEqual(res['stored'], 1)
        med = self.by_uid(MEDICATION)
        self.assertEqual([r['status'] for r in med], ['confirmed'] * 3)
        self.assertEqual(med[1]['utc'], '2026-03-29T08:00:00+00:00')
        # the breakfast series keeps its own 11:00 replacement, untouched
        self.assertEqual(self.by_uid(BREAKFAST)[1]['local'], '2026-03-29T11:00:00')
        # authorized UTC-form cancel targets exactly that one meal
        ingest_ics(self.con, UTC_CANCEL_MEDICATION, ORG)
        med = self.by_uid(MEDICATION)
        self.assertEqual([r['status'] for r in med],
                         ['confirmed', 'cancelled', 'confirmed'])
        self.assertEqual(med[1]['utc'], '2026-03-29T08:00:00+00:00')

    # --- no attestation: the parser must not choose a semantics -------------

    def test_unattested_series_stays_pending(self):
        ingest_ics(self.con, self.ics, ORG)
        rows = self.by_uid(UNATTESTED)
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0]['utc'], rows[0]['status']),
                         ('2026-03-22T08:00:00+00:00', 'pending'))
        self.assertEqual((rows[1]['local'], rows[1]['utc'], rows[1]['status']),
                         (None, None, 'pending'))
        self.assertEqual([w for w in windows(self.con) if w['uid'] == UNATTESTED], [])

    def test_unknown_time_mode_rejected_atomically(self):
        bad = self.ics.replace('X-MEAL-TIME-MODE:WALLCLOCK', 'X-MEAL-TIME-MODE:GUESS', 1)
        with self.assertRaises(ValueError):
            ingest_ics(self.con, bad, ORG)
        self.assertEqual(instances(self.con), [])

    def test_floating_time_rejected_not_guessed(self):
        bad = self.ics.replace('DTSTART;TZID=Europe/Berlin:20260322T090000',
                               'DTSTART:20260322T090000', 1)
        with self.assertRaises(ValueError):
            ingest_ics(self.con, bad, ORG)
        self.assertEqual(instances(self.con), [])

    # --- ambiguous / nonexistent wall clocks keep every possibility ---------

    def test_ambiguous_fold_keeps_both_instants(self):
        ingest_ics(self.con, self.ics, ORG)
        wk1, wk2, wk3 = self.by_uid(AUTUMN)
        self.assertEqual((wk1['utc'], wk1['status'], wk1['fold']),
                         ('2026-10-18T00:30:00+00:00', 'confirmed', 0))
        self.assertEqual((wk2['status'], wk2['utc'], wk2['fold']), ('ambiguous', None, None))
        self.assertEqual(json.loads(wk2['candidates']),
                         [{'utc': '2026-10-25T00:30:00+00:00', 'fold': 0},
                          {'utc': '2026-10-25T01:30:00+00:00', 'fold': 1}])
        self.assertEqual((wk3['utc'], wk3['status']),
                         ('2026-11-01T01:30:00+00:00', 'confirmed'))

    def test_gap_wall_clock_marked_not_guessed(self):
        ingest_ics(self.con, self.ics, ORG)
        wk1, wk2 = self.by_uid(GAP)
        self.assertEqual((wk1['utc'], wk1['status']), ('2026-03-22T01:30:00+00:00', 'confirmed'))
        self.assertEqual((wk2['status'], wk2['utc'], json.loads(wk2['candidates'])),
                         ('gap', None, []))

    # --- ordering, replay, idempotency ---------------------------------------

    def test_cancel_before_series_gives_same_table(self):
        res = ingest_ics(self.con, CANCEL_BREAKFAST_WK3, ORG)
        self.assertEqual(res['stored'], 1)
        self.assertEqual(instances(self.con), [])  # nothing expandable yet
        ingest_ics(self.con, self.ics, ORG)
        early = instances(self.con)
        wk3 = [r for r in early if r['rkey'] == 'local|Europe/Berlin|2026-04-05T09:00:00'][0]
        self.assertEqual(wk3['status'], 'cancelled')
        others = [r for r in self.by_uid(BREAKFAST) if r['seq'] != 2]
        self.assertEqual([r['status'] for r in others], ['confirmed', 'confirmed'])
        # series-first order converges to the identical instance table
        other = storage.connect(':memory:')
        try:
            ingest_ics(other, self.ics, ORG)
            ingest_ics(other, CANCEL_BREAKFAST_WK3, ORG)
            self.assertEqual(instances(other), early)
        finally:
            other.close()
        # historical replay: re-materializing stored versions is a no-op
        materialize(self.con)
        self.assertEqual(instances(self.con), early)

    def test_reimport_and_offline_resume_idempotent(self):
        first = ingest_ics(self.con, self.ics, ORG)
        self.assertEqual(first['stored'], first['seen'])
        snapshot = instances(self.con)
        again = ingest_ics(self.con, self.ics, ORG)
        self.assertEqual(again['stored'], 0)
        self.assertEqual(instances(self.con), snapshot)
        # offline resume: a truncated transfer finished later completes the
        # same table as the one-shot import
        marker = 'BEGIN:VEVENT'
        cut = self.ics.index(marker, self.ics.index(marker, self.ics.index(marker) + 1) + 1)
        other = storage.connect(':memory:')
        try:
            part1 = ingest_ics(other, self.ics[:cut], ORG)
            part2 = ingest_ics(other, self.ics[cut:], ORG)
            self.assertEqual((part1['stored'], part2['stored']), (2, 4))
            self.assertEqual(instances(other), snapshot)
        finally:
            other.close()

    # --- cross-institution isolation ------------------------------------------

    def test_cross_institution_same_uid_never_overwrites(self):
        ingest_ics(self.con, self.ics, 'org-a')
        ingest_ics(self.con, self.ics, 'org-b')
        a, b = instances(self.con, 'org-a'), instances(self.con, 'org-b')
        self.assertTrue(a and b)
        self.assertEqual(len(a), len(b))
        self.assertFalse({r['id'] for r in a} & {r['id'] for r in b})
        self.assertTrue(all(r['series_id'].startswith('org-a:') for r in a))
        self.assertTrue(all(r['series_id'].startswith('org-b:') for r in b))
        strip = lambda rows: sorted((r['uid'], r['seq'], r['utc'], r['status']) for r in rows)
        self.assertEqual(strip(a), strip(b))

    # --- downstream meal preparation is not doubled ----------------------------

    def go(self, eid, action, **data):
        return cli.run_event(self.con, 'service',
                             {'id': eid, 'subject': 'senior-a', 'action': action,
                              'data': data, 'effective_at': 1, 'received_at': 1})

    def enroll(self):
        return cli.run_event(self.con, 'eligibility',
                             {'id': 'e0', 'subject': 'senior-a', 'action': 'enroll',
                              'data': {'monthly_limit': 30}, 'effective_at': 1, 'received_at': 1})

    def test_service_prepare_pickup_instance_flow(self):
        ingest_ics(self.con, self.ics, ORG)
        self.enroll()
        wk2 = [w for w in windows(self.con) if w['uid'] == BREAKFAST and w['rescheduled']][0]
        prep = self.go('s1', 'prepare', meal='breakfast', stock=2, orders=1, instance=wk2['id'])
        self.assertEqual(prep['outcome'], 'prepared')
        dup = self.go('s2', 'prepare', meal='breakfast', stock=2, orders=1, instance=wk2['id'])
        self.assertEqual((dup['outcome'], dup['reason']), ('duplicate', 'instance-already-prepared'))
        pick = self.go('s3', 'pickup', prepare='s1', instance=wk2['id'])
        self.assertEqual(pick['outcome'], 'picked-up')
        # the medication meal the same week is a different instance: independent
        med2 = [w for w in windows(self.con) if w['uid'] == MEDICATION and w['seq'] == 1][0]
        prep2 = self.go('s4', 'prepare', meal='medication-meal', stock=2, orders=1, instance=med2['id'])
        self.assertEqual(prep2['outcome'], 'prepared')
        mismatch = self.go('s5', 'pickup', prepare='s4', instance=wk2['id'])
        self.assertEqual((mismatch['outcome'], mismatch['reason']), ('blocked', 'instance-mismatch'))
        # a cross-DST cancel of week 3 blocks only that meal's preparation
        ingest_ics(self.con, CANCEL_BREAKFAST_WK3, ORG)
        wk3 = [r for r in self.by_uid(BREAKFAST) if r['seq'] == 2][0]
        cancelled = self.go('s6', 'prepare', meal='breakfast', stock=2, orders=1, instance=wk3['id'])
        self.assertEqual((cancelled['outcome'], cancelled['reason']), ('blocked', 'instance-cancelled'))
        # ambiguous / pending / unknown instances are never prepared
        amb = [r for r in self.by_uid(AUTUMN) if r['status'] == 'ambiguous'][0]
        self.assertEqual(self.go('s7', 'prepare', meal='brunch', stock=2, orders=1,
                                 instance=amb['id'])['reason'], 'instance-ambiguous')
        pend = self.by_uid(UNATTESTED)[0]
        self.assertEqual(self.go('s8', 'prepare', meal='breakfast', stock=2, orders=1,
                                 instance=pend['id'])['reason'], 'instance-pending')
        self.assertEqual(self.go('s9', 'prepare', meal='breakfast', stock=2, orders=1,
                                 instance='org-a:nope#x')['reason'], 'unknown-pickup-instance')
        # legacy flow without an instance reference still works
        plain = self.go('s10', 'prepare', meal='lunch', stock=2, orders=1)
        self.assertEqual(plain['outcome'], 'prepared')


if __name__ == '__main__':
    unittest.main()

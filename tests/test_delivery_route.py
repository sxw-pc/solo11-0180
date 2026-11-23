"""配送实际道路路径与里程证书验收。

覆盖：
1. 单点 trace 副本基线（A/B/A）；
2. 朴素完整管道把不存在的 A→B 跳路当成已签轨迹（缺 C/D、拓扑无效、照记 10km）；
3. 道路图约束出的两条真实候选（直行 A 10km / A→C→B→D→A 14km）的速度与拓扑证书；
4. B 路双签停靠 → 14km，A 路相机+里程表 → 10km，两路保留同一原包与标签；
5. 晚到证据只追加更正，不改写原 GPS、原签名与原证书。
"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from mealassist import storage
from mealassist.gps import (
    compatible_roads,
    denoise,
    packet_digest,
    sign_digest,
    snap,
    verify_packet,
)
from mealassist.route_capture import (
    ambiguity_report,
    candidate_routes,
    certify_b_route,
    certify_direct_a,
    enumerate_paths,
    naive_pipeline,
    record_correction,
    register_handovers,
    trace,
)

FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures' / 'delivery-route.json'


def load_trip():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


class SinglePointTraceBaseline(unittest.TestCase):
    def test_seed_style_baseline_still_returns_aba(self):
        points = [
            {'candidates': [{'road': 'A', 'distance_m': 1},
                            {'road': 'B', 'distance_m': 8}]},
            {'candidates': [{'road': 'A', 'distance_m': 6},
                            {'road': 'B', 'distance_m': 2}]},
            {'candidates': [{'road': 'A', 'distance_m': 1},
                            {'road': 'B', 'distance_m': 7}]},
        ]
        self.assertEqual(trace(points), ['A', 'B', 'A'])

    def test_fixture_single_point_snaps_are_aba_but_ellipse_keeps_both(self):
        trip = load_trip()
        self.assertEqual([snap(p) for p in trip['gps_packets']], ['A', 'B', 'A'])
        self.assertEqual(trace(trip['gps_packets']), ['A', 'B', 'A'])
        mid = trip['gps_packets'][1]
        self.assertEqual(sorted(compatible_roads(mid)), ['A', 'B'])
        self.assertEqual(mid['candidates'][0]['distance_m'], 6)
        self.assertEqual(mid['candidates'][1]['distance_m'], 2)

    def test_raw_packet_signatures_verify_and_tampering_is_detected(self):
        trip = load_trip()
        for packet in trip['gps_packets']:
            ok, reason = verify_packet(packet, trip['devices']['dev-1'])
            self.assertTrue(ok, reason)
        tampered = json.loads(json.dumps(trip['gps_packets'][1]))
        tampered['x_m'] = 999
        ok, reason = verify_packet(tampered, trip['devices']['dev-1'])
        self.assertFalse(ok)
        self.assertEqual(reason, 'packet-digest-mismatch')
        forged = json.loads(json.dumps(trip['gps_packets'][1]))
        forged['device_signature'] = '0' * len(forged['device_signature'])
        ok, reason = verify_packet(forged, trip['devices']['dev-1'])
        self.assertFalse(ok)
        self.assertEqual(reason, 'device-signature-invalid')

    def test_denoise_drops_clock_failure_but_keeps_verified_packets(self):
        trip = load_trip()
        bad = json.loads(json.dumps(trip['gps_packets'][1]))
        bad['id'] = 'gps-bad-clock'
        bad['clock'] = {'offset_s': 9999, 'drift_ppm': 5}
        digest = packet_digest(bad)
        bad['packet_sha256'] = digest
        bad['device_signature'] = sign_digest(digest,
                                              trip['devices']['dev-1']['secret'])
        packets = [trip['gps_packets'][0], bad, trip['gps_packets'][2]]
        kept, rejected = denoise(packets, trip['devices'])
        self.assertEqual([p['id'] for p in kept], ['gps-1', 'gps-3'])
        self.assertEqual(rejected, [{'id': 'gps-bad-clock',
                                     'reason': 'clock-offset-out-of-tolerance'}])


class NaivePipelineIsRejected(unittest.TestCase):
    def test_direct_snap_chain_is_a_nonexistent_jump(self):
        trip = load_trip()
        report = naive_pipeline(trip)
        self.assertEqual(report['snap_labels'], ['A', 'B', 'A'])
        self.assertFalse(report['topology_valid'])
        self.assertFalse(report['certified'])
        self.assertEqual(
            [(link['from_road'], link['to_road'], link['connected'])
             for link in report['links']],
            [('A', 'B', False), ('B', 'A', False)],
        )

    def test_naive_pipeline_omits_cd_but_still_books_ten_km(self):
        trip = load_trip()
        report = naive_pipeline(trip)
        self.assertTrue(any('missing-connectors-C-D' in d for d in report['defects']))
        # 按原包观测位置直线记程，恰好把不存在的 A→B→A 跳路记成约 10km
        self.assertAlmostEqual(report['claimed_distance_m'], 10000.0, delta=20.0)

    def test_turn_restriction_prunes_the_forbidden_manoeuvre(self):
        trip = load_trip()
        network = trip['network']
        # 不应用转向限制时，C 末端可转 SHORTCUT 形成第三条路径
        unrestricted = json.loads(json.dumps(network))
        unrestricted['turn_restrictions'] = []
        raw_paths = enumerate_paths(unrestricted, 'depot', 'station')
        self.assertTrue(any(
            ['e-c', 'e-shortcut'] == [e['id'] for e in path] for path in raw_paths
        ))
        restricted = enumerate_paths(network, 'depot', 'station')
        self.assertFalse(any(
            any(e['id'] == 'e-shortcut' for e in path) for path in restricted
        ))


class CandidateRouteCertificates(unittest.TestCase):
    def setUp(self):
        self.trip = load_trip()
        self.report = candidate_routes(self.trip)

    def test_graph_constraint_yields_exactly_two_feasible_routes(self):
        self.assertTrue(self.report['ambiguous'])
        roads = [tuple(c['roads']) for c in self.report['candidates']]
        self.assertEqual(roads, [('A',), ('C', 'B', 'D')])
        distances = [c['distance_m'] for c in self.report['candidates']]
        self.assertEqual(distances, [10000, 14000])
        self.assertEqual(self.report['rejected_paths'], [])

    def test_both_routes_satisfy_clock_speed_and_gps_evidence(self):
        by_roads = {tuple(c['roads']): c for c in self.report['candidates']}
        for roads, speed in ((('A',), 15.0), (('C', 'B', 'D'), 21.0)):
            candidate = by_roads[roads]
            self.assertTrue(candidate['gps_compatible'])
            self.assertEqual(candidate['departure_ts'], 32400)
            self.assertEqual(candidate['arrival_ts'], 34800)
            self.assertLessEqual(candidate['required_speed_kmh'],
                                 candidate['max_speed_kmh'])
            self.assertAlmostEqual(candidate['required_speed_kmh'], speed, places=2)
            for check in candidate['interval_checks']:
                self.assertEqual(check['elapsed_s'], 1200)
                self.assertTrue(check['within_speed_limit'])
                self.assertLessEqual(check['progress_m'], check['max_distance_m'])

    def test_candidates_keep_their_own_required_signoffs(self):
        by_roads = {tuple(c['roads']): c for c in self.report['candidates']}
        b_types = [x['type'] for x in by_roads[('C', 'B', 'D')]['required_signoffs']]
        a_types = [x['type'] for x in by_roads[('A',)]['required_signoffs']]
        self.assertIn('b-route-stop-double-sign', b_types)
        self.assertIn('station-arrival-handover', b_types)
        self.assertNotIn('b-route-stop-double-sign', a_types)
        self.assertIn('odometer-camera-drift-evidence', a_types)

    def test_ambiguity_is_not_resolved_by_picking_max_likelihood(self):
        outcome = ambiguity_report(self.report)
        self.assertFalse(outcome['certified'])
        self.assertEqual(len(outcome['candidate_ids']), 2)
        self.assertEqual(set(outcome['candidate_distances_m'].values()),
                         {10000, 14000})

    def test_speed_limit_violation_rejects_candidate(self):
        trip = json.loads(json.dumps(self.trip))
        trip['max_speed_kmh'] = 10  # 两条路线分别需要 15/21km/h
        outcome = candidate_routes(trip)
        self.assertEqual(outcome['candidates'], [])
        self.assertTrue(all(
            item['reason'] == 'time-or-speed-constraint-violated'
            for item in outcome['rejected_paths']
        ))


class BRouteStopAndAOdometreCertificates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = storage.connect(Path(self.tmp.name) / 'route.db')
        self.trip = load_trip()
        self.report = candidate_routes(self.trip)
        for packet in self.trip['gps_packets']:
            storage.save_gps_packet(self.con, self.trip['trip_id'], packet)

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_double_signed_b_stop_certifies_fourteen_km(self):
        stop = self.trip['signed_stops'][0]
        result = certify_b_route(
            self.con, self.trip, self.report, stop,
            {k: self.trip['signer_secrets'][k] for k in ('driver', 'station')},
        )
        self.assertTrue(result['certified'])
        cert = result['certificate']
        self.assertEqual(cert['distance_m'], 14000)
        self.assertEqual(cert['roads'], ['C', 'B', 'D'])
        self.assertEqual(cert['basis'], 'b-route-double-signed-stop')
        self.assertEqual(cert['raw_snap_labels'], ['A', 'B', 'A'])
        self.assertEqual(cert['raw_packet_ids'], ['gps-1', 'gps-2', 'gps-3'])
        self.assertTrue(cert['cert_digest'])
        rows = register_handovers(self.con, self.trip, cert,
                                  self.trip['handovers'], self.report['candidates'])
        self.assertEqual([(r['handover_id'], r['state']) for r in rows],
                         [('ho-mid-b', 'verified'), ('ho-station', 'verified')])
        stored = storage.latest_route_certificate(self.con, self.trip['trip_id'])
        self.assertEqual(stored['distance_m'], 14000)

    def test_forged_b_stop_is_rejected(self):
        stop = json.loads(json.dumps(self.trip['signed_stops'][0]))
        stop['signatures']['driver'] = 'f' * 64
        result = certify_b_route(
            self.con, self.trip, self.report, stop,
            {k: self.trip['signer_secrets'][k] for k in ('driver', 'station')},
        )
        self.assertFalse(result['certified'])
        self.assertEqual(result['reason'], 'driver-signature-invalid')

    def test_camera_and_odometre_certify_direct_a_ten_km(self):
        evidence = self.trip['evidence']['direct_a']
        result = certify_direct_a(self.con, self.trip, self.report,
                                  evidence, self.trip['signer_secrets'])
        self.assertTrue(result['certified'])
        cert = result['certificate']
        self.assertEqual(cert['distance_m'], 10000)
        self.assertEqual(cert['roads'], ['A'])
        self.assertEqual(cert['basis'], 'odometer-camera-drift-evidence')
        self.assertEqual(cert['raw_snap_labels'], ['A', 'B', 'A'])
        rows = register_handovers(self.con, self.trip, cert,
                                  self.trip['handovers'], self.report['candidates'])
        # 车辆未到 B，B 上的停靠不得当成交接；站点交接可认领
        self.assertEqual([(r['handover_id'], r['state'], r['reason']) for r in rows],
                         [('ho-mid-b', 'rejected',
                           'stop-road-not-on-certified-route'),
                          ('ho-station', 'verified', 'handover-verified')])

    def test_odometre_mismatch_blocks_certificate(self):
        from mealassist.gps import sign_digest
        evidence = json.loads(json.dumps(self.trip['evidence']['direct_a']))
        odo = evidence['odometer']
        odo['end_reading_m'] = 130060  # 实际读数 10060m，超出 50m 容差
        digest = hashlib.sha256(json.dumps(
            {k: v for k, v in odo.items() if k != 'signatures'},
            ensure_ascii=False, sort_keys=True, separators=(',', ':')
        ).encode('utf-8')).hexdigest()
        odo['signatures'] = {
            'vehicle': sign_digest(digest, self.trip['signer_secrets']['vehicle'])
        }
        result = certify_direct_a(self.con, self.trip, self.report,
                                  evidence, self.trip['signer_secrets'])
        self.assertFalse(result['certified'])
        self.assertEqual(result['reason'], 'odometer-distance-mismatch')
        self.assertEqual(result['odometer_m'], 10060)
        self.assertEqual(result['candidate_m'], 10000)

    def test_tampered_odometre_signature_blocks_certificate(self):
        evidence = json.loads(json.dumps(self.trip['evidence']['direct_a']))
        evidence['odometer']['signatures']['vehicle'] = 'b' * 64
        result = certify_direct_a(self.con, self.trip, self.report,
                                  evidence, self.trip['signer_secrets'])
        self.assertFalse(result['certified'])
        self.assertEqual(result['reason'], 'vehicle-signature-invalid')

    def test_two_certificates_coexist_and_raw_evidence_is_immutable(self):
        certify_b_route(
            self.con, self.trip, self.report, self.trip['signed_stops'][0],
            {k: self.trip['signer_secrets'][k] for k in ('driver', 'station')},
        )
        certify_direct_a(self.con, self.trip, self.report,
                         self.trip['evidence']['direct_a'],
                         self.trip['signer_secrets'])
        rows = self.con.execute(
            'SELECT version,distance_m FROM route_certificates ORDER BY version'
        ).fetchall()
        self.assertEqual([(r['version'], r['distance_m']) for r in rows],
                         [(1, 14000), (2, 10000)])
        # 原始 GPS 原包与签名始终未被改写
        stored_packets = self.con.execute(
            'SELECT id,digest FROM gps_packets ORDER BY id'
        ).fetchall()
        for row, packet in zip(stored_packets, self.trip['gps_packets']):
            self.assertEqual(row['digest'], packet_digest(packet))
        latest = storage.latest_route_certificate(self.con, self.trip['trip_id'])
        self.assertEqual(latest['raw_snap_labels'], ['A', 'B', 'A'])

    def test_late_evidence_appends_correction_without_rewriting_trace(self):
        result = certify_b_route(
            self.con, self.trip, self.report, self.trip['signed_stops'][0],
            {k: self.trip['signer_secrets'][k] for k in ('driver', 'station')},
        )
        cert = result['certificate']
        prior_digest = cert['cert_digest']
        late = self.trip['evidence']['late_arrival_claim']
        correction = record_correction(
            self.con, self.trip, prior_digest, 'late-arrival-evidence', late,
            'late-evidence-kept-separately-original-signed-trace-unchanged',
        )
        stored = self.con.execute('SELECT * FROM route_corrections').fetchall()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]['prior_cert_digest'], prior_digest)
        self.assertEqual(stored[0]['kind'], 'late-arrival-evidence')
        # 原证书与原 GPS 原包都还在，且证书摘要未变
        self.assertEqual(
            self.con.execute(
                'SELECT cert_digest FROM route_certificates WHERE id=? AND version=1',
                (cert['cert_id'],)).fetchone()['cert_digest'],
            prior_digest,
        )
        self.assertEqual(correction['prior_cert_digest'], prior_digest)
        packet_count = self.con.execute('SELECT COUNT(*) n FROM gps_packets').fetchone()['n']
        self.assertEqual(packet_count, 3)

    def test_late_handover_is_gated_but_original_signed_trace_stays(self):
        from mealassist import service
        result = certify_b_route(
            self.con, self.trip, self.report, self.trip['signed_stops'][0],
            {k: self.trip['signer_secrets'][k] for k in ('driver', 'station')},
        )
        cert = result['certificate']
        # 声称 09:10 已到站交接：B 路线 14km 在限速 30 下至少需要 28 分钟，
        # 该时刻物理不可达，交接必须被拒（晚到证据另走更正，不改原轨迹）
        late = {
            'handover_id': 'ho-late', 'kind': 'station-arrival', 'road': 'A',
            'observed_ts': 33000,
            'box_id': 'box-late', 'senior_id': 'senior-late',
        }
        digest = hashlib.sha256(json.dumps(
            late, ensure_ascii=False, sort_keys=True, separators=(',', ':')
        ).encode('utf-8')).hexdigest()
        late['signatures'] = {
            role: sign_digest(digest, self.trip['signer_secrets'][role])
            for role in ('driver', 'station')
        }
        rows = register_handovers(self.con, self.trip, cert, [late],
                                  self.report['candidates'])
        self.assertEqual(rows[0]['state'], 'rejected')
        self.assertEqual(rows[0]['reason'], 'arrival-time-infeasible')
        # 09:45 才送达的晚到证据只追加更正
        record_correction(
            self.con, self.trip, cert['cert_digest'], 'late-arrival-evidence',
            self.trip['evidence']['late_arrival_claim'],
            'late-station-handover-under-review',
        )
        # 被拒交接不能供 pickup 认领
        self.con.execute(
            "INSERT INTO subjects VALUES(?,?,?,?)",
            ('senior-late', 'senior', 'eligible', 1))
        prep = {'id': 'prep-late', 'subject': 'senior-late', 'action': 'prepare',
                'data': {'meal': 'lunch', 'stock': 1, 'orders': 1},
                'effective_at': 1, 'received_at': 1}
        self.assertEqual(service.apply(self.con, prep)['outcome'], 'prepared')
        pickup = {'id': 'pick-late', 'subject': 'senior-late', 'action': 'pickup',
                  'data': {'prepare': 'prep-late', 'delivery': 'ho-late'},
                  'effective_at': 2, 'received_at': 2}
        decision = service.apply(self.con, pickup)
        self.assertEqual(decision['outcome'], 'blocked')
        self.assertTrue(decision['reason'].startswith('delivery-not-verified'))
        # 原 GPS 标签与司机签名仍完整保留在证书与原包中
        latest = storage.latest_route_certificate(self.con, self.trip['trip_id'])
        self.assertEqual(latest['raw_snap_labels'], ['A', 'B', 'A'])
        digests = {r['id']: r['digest']
                   for r in self.con.execute('SELECT id,digest FROM gps_packets')}
        for packet in self.trip['gps_packets']:
            self.assertEqual(digests[packet['id']], packet_digest(packet))
        from mealassist.route_capture import _object_digest
        stop = self.trip['signed_stops'][0]
        self.assertEqual(cert['stop']['digest'], _object_digest(stop))
        # 司机原始签名对象本身没有被证书更正流程改动
        self.assertTrue(stop['signatures']['driver'])


class PickupConsumesVerifiedHandovers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = storage.connect(Path(self.tmp.name) / 'service.db')
        self.trip = load_trip()
        self.report = candidate_routes(self.trip)
        self.event_seq = 0

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def _event(self, action, subject, **data):
        self.event_seq += 1
        return {
            'id': f'e{self.event_seq}', 'subject': subject, 'action': action,
            'data': data, 'effective_at': self.event_seq,
            'received_at': self.event_seq,
        }

    def _enroll_and_prepare(self, senior):
        from mealassist import service
        self.con.execute(
            "INSERT INTO subjects VALUES(?,?,?,?)", (senior, 'senior', 'eligible', 1))
        prep_event = self._event('prepare', senior, meal='lunch', stock=2, orders=1)
        decision = service.apply(self.con, prep_event)
        self.assertEqual(decision['outcome'], 'prepared')
        return prep_event['id']

    def _pickup(self, senior, prepare_id, delivery=None, box=None):
        from mealassist import service
        data = {'prepare': prepare_id}
        if delivery:
            data['delivery'] = delivery
        if box:
            data['box'] = box
        return service.apply(self.con, self._event('pickup', senior, **data))

    def test_pickup_requires_verified_station_handover(self):
        cert = certify_direct_a(
            self.con, self.trip, self.report,
            self.trip['evidence']['direct_a'], self.trip['signer_secrets'],
        )['certificate']
        register_handovers(self.con, self.trip, cert,
                           self.trip['handovers'], self.report['candidates'])
        prepare_id = self._enroll_and_prepare('senior-1')
        # 他人凭据 / 不存在的凭据不能认领
        foreign = self._pickup('senior-1', prepare_id, delivery='ho-mid-b')
        self.assertEqual(foreign['reason'], 'delivery-credential-unknown')
        unknown = self._pickup('senior-1', prepare_id, delivery='ho-missing')
        self.assertEqual(unknown['reason'], 'delivery-credential-unknown')
        # 同一长者的被拒凭据（构造一个主体为 senior-1 的未核实交接）
        self.con.execute(
            "INSERT OR REPLACE INTO delivery_handovers VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ('ho-rejected', self.trip['trip_id'], cert['cert_id'], 'senior-1',
             'box-9', 'mid-route-stop', 'B', 33600, 'rejected',
             'stop-road-not-on-certified-route', 'x'))
        blocked = self._pickup('senior-1', prepare_id, delivery='ho-rejected')
        self.assertEqual(blocked['outcome'], 'blocked')
        self.assertTrue(blocked['reason'].startswith('delivery-not-verified'))
        # 经核实的到站交接可以认领
        ok = self._pickup('senior-1', prepare_id,
                          delivery='ho-station', box='box-1')
        self.assertEqual(ok['outcome'], 'picked-up')
        self.assertEqual(ok['reason'], 'recipient-identity-checked')

    def test_pickup_rejects_box_mismatch(self):
        cert = certify_direct_a(
            self.con, self.trip, self.report,
            self.trip['evidence']['direct_a'], self.trip['signer_secrets'],
        )['certificate']
        register_handovers(self.con, self.trip, cert,
                           self.trip['handovers'], self.report['candidates'])
        prepare_id = self._enroll_and_prepare('senior-1')
        decision = self._pickup('senior-1', prepare_id,
                                delivery='ho-station', box='box-other')
        self.assertEqual(decision['outcome'], 'blocked')
        self.assertEqual(decision['reason'], 'delivery-box-mismatch')

    def test_legacy_pickup_without_delivery_credential_unchanged(self):
        prepare_id = self._enroll_and_prepare('senior-legacy')
        decision = self._pickup('senior-legacy', prepare_id)
        self.assertEqual(decision['outcome'], 'picked-up')

    def test_claimable_meals_differ_between_the_two_certificates(self):
        # B 证书：中途与站点交接均核实，两个餐盒可认领
        b_cert = certify_b_route(
            self.con, self.trip, self.report, self.trip['signed_stops'][0],
            {k: self.trip['signer_secrets'][k] for k in ('driver', 'station')},
        )['certificate']
        b_rows = register_handovers(self.con, self.trip, b_cert,
                                    self.trip['handovers'],
                                    self.report['candidates'])
        self.assertEqual([r['state'] for r in b_rows], ['verified', 'verified'])
        # A 证书（新库，避免交接 ID 冲突）
        con_a = storage.connect(':memory:')
        a_cert = certify_direct_a(
            con_a, self.trip, self.report,
            self.trip['evidence']['direct_a'], self.trip['signer_secrets'],
        )['certificate']
        a_rows = register_handovers(con_a, self.trip, a_cert,
                                    self.trip['handovers'],
                                    self.report['candidates'])
        self.assertEqual([r['state'] for r in a_rows], ['rejected', 'verified'])
        con_a.close()


if __name__ == '__main__':
    unittest.main()

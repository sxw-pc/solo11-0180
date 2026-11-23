"""实际道路路径重建与里程证书。

基线 ``trace`` 逐点调用 :func:`mealassist.gps.snap`，只产出一串最近道路标签，
不读取道路连接关系。本模块在保留该基线的同时，新增证据驱动的路径重建：

1. 在有向道路图上枚举仓到站的全部简单路径，应用路口转向限制；
2. 用 GPS 原包误差椭圆做道路相容、用采样时钟做相邻点时间/速度约束，
   只保留真实可通行的候选路径，保留每条候选的里程、到站时间与所需签认；
3. 证书签发必须由外部核实证据（B 路双签停靠或相机/里程表漂移证明）选定
   唯一候选；证据不足时只给歧义报告，不随手挑最高似然值；
4. 证书更正只追加 ``route_corrections``，不抹去原 GPS 原包、标签与司机签名。
"""
import hashlib
import hmac
import json
import math
from itertools import product

from . import storage as s
from .gps import (
    adjust_clock,
    compatible_roads,
    denoise,
    sign_digest,
    snap,
)


def trace(points):
    """基线：逐点取最近道路标签（保持不变）。"""
    return [snap(point) for point in points]


# ---------------------------------------------------------------------------
# 道路有向拓扑与转向限制
# ---------------------------------------------------------------------------

def _adjacency(network):
    adj = {}
    for edge in network['edges']:
        adj.setdefault(edge['from_node'], []).append(edge)
    return adj


def _forbidden_turns(network):
    return {
        (r['at_node'], r['from_edge'], r['to_edge'])
        for r in network.get('turn_restrictions', [])
        if r.get('kind', 'forbidden') == 'forbidden'
    }


def enumerate_paths(network, start_node, goal_node):
    """枚举有向图上 start→goal 的全部简单路径，应用路口转向限制。

    转向限制是边对级别的（在前一条边上、在路口节点、禁止转入下一条边），
    节点不重复访问以避免在环路里兜圈。
    """
    adj = _adjacency(network)
    forbidden = _forbidden_turns(network)
    results = []

    def walk(node, path_edges, visited):
        if node == goal_node:
            results.append(list(path_edges))
            return
        for edge in adj.get(node, []):
            nxt = edge['to_node']
            if nxt in visited:
                continue
            if path_edges and (node, path_edges[-1]['id'], edge['id']) in forbidden:
                continue
            path_edges.append(edge)
            visited.add(nxt)
            walk(nxt, path_edges, visited)
            visited.remove(nxt)
            path_edges.pop()

    walk(start_node, [], {start_node})
    return results


def road_adjacency(network):
    """道路标签级别的有向邻接：路 a 的边末端能否直接接上含路 b 的边。"""
    edges = network['edges']
    links = set()
    for first in edges:
        for second in edges:
            if first['to_node'] == second['from_node']:
                links.add((first['road'], second['road']))
    return links


def path_distance(path_edges):
    return sum(int(e['length_m']) for e in path_edges)


# ---------------------------------------------------------------------------
# 朴素完整管道（验签、去噪、按点取最近路、相邻标签直连）——对照用，不发证书
# ---------------------------------------------------------------------------

def _euclid(a, b):
    return math.hypot(float(a['x_m']) - float(b['x_m']),
                      float(a['y_m']) - float(b['y_m']))


def naive_pipeline(trip):
    """复现“按点取最近路并把相邻标签直连”的普通管道。

    它会把图上不存在的 A→B 跳路当成已签轨迹，按原包观测位置直线记程，
    且完全遗漏 C/D 连接道。返回结构自带拓扑缺陷，供责任核查对照。
    """
    kept, rejected = denoise(trip['gps_packets'], trip['devices'])
    labels = [snap(packet) for packet in kept]
    links_set = road_adjacency(trip['network'])
    links, defects, claimed = [], [], 0.0
    for idx, (a, b) in enumerate(zip(labels, labels[1:])):
        connected = (a == b) or ((a, b) in links_set)
        links.append({'from_road': a, 'to_road': b, 'connected': connected})
        if not connected:
            defects.append(f'disconnected-snap-{a}->{b}')
        claimed += _euclid(kept[idx], kept[idx + 1])
    missing = sorted(dict.fromkeys(
        edge['road'] for edge in trip['network']['edges']
        if edge['road'] not in labels and int(edge['length_m']) > 0
        and not edge.get('abstract', False)
    ))
    if missing:
        defects.append('missing-connectors-' + '-'.join(missing))
    return {
        'kept_packet_ids': [p['id'] for p in kept],
        'rejected': rejected,
        'snap_labels': labels,
        'links': links,
        'topology_valid': all(link['connected'] for link in links),
        'claimed_distance_m': round(claimed, 3),
        'certified': False,
        'defects': defects,
    }


# ---------------------------------------------------------------------------
# 候选路径：GPS 相容 + 采样时钟 + 时间/速度约束
# ---------------------------------------------------------------------------

def _normalized_samples(trip):
    kept, rejected = denoise(trip['gps_packets'], trip['devices'])
    kept.sort(key=lambda p: (adjust_clock(p), p['id']))
    samples = []
    for idx, packet in enumerate(kept):
        samples.append({
            'packet_id': packet['id'],
            'index': idx,
            'ts': adjust_clock(packet),
            'nearest_road': snap(packet),
            'compatible_roads': compatible_roads(packet),
            'node': packet.get('node'),
            'candidates': packet['candidates'],
        })
    return samples, rejected


def _anchors_for_sample(sample, path_edges, edge_offsets, total_m):
    """样本在该路径上的可行进度锚点（米）。端点原包锚定节点，其余按相容道路投影。"""
    anchors = []
    if sample['node']:
        if sample['node'] == path_edges[0]['from_node']:
            anchors.append((0, path_edges[0]['road']))
        if sample['node'] == path_edges[-1]['to_node']:
            anchors.append((total_m, path_edges[-1]['road']))
    for road in sample['compatible_roads']:
        for idx, edge in enumerate(path_edges):
            if edge['road'] != road:
                continue
            match = next((c for c in sample['candidates'] if c['road'] == road), None)
            if match is None:
                continue
            along = int(match.get('along_m', edge['length_m']))
            if 0 <= along <= int(edge['length_m']):
                anchors.append((edge_offsets[idx] + along, road))
    # 去重并保持稳定顺序
    unique = []
    for anchor in anchors:
        if anchor not in unique:
            unique.append(anchor)
    return unique


def _interval_checks(samples, anchor_choice, vmax_ms):
    checks = []
    feasible = True
    for i in range(len(samples) - 1):
        dt = samples[i + 1]['ts'] - samples[i]['ts']
        d = anchor_choice[i + 1][0] - anchor_choice[i][0]
        ok = dt > 0 and -1e-9 <= d <= dt * vmax_ms + 1e-9
        checks.append({
            'from_packet': samples[i]['packet_id'],
            'to_packet': samples[i + 1]['packet_id'],
            'elapsed_s': dt,
            'progress_m': round(d, 3),
            'max_distance_m': round(dt * vmax_ms, 3),
            'within_speed_limit': ok,
        })
        feasible = feasible and ok
    return feasible, checks


def candidate_routes(trip):
    """枚举并检核全部与证据相容的候选路径。

    不做唯一点选择：只要通过拓扑、GPS 误差椭圆和时间/速度约束就保留，
    并附上各自里程、到站时间和所需签认。
    """
    network = trip['network']
    vmax_kmh = float(trip['max_speed_kmh'])
    vmax_ms = vmax_kmh / 3.6
    samples, rejected = _normalized_samples(trip)
    if len(samples) < 2:
        raise ValueError('not-enough-verified-packets')
    start_node = samples[0]['node']
    goal_node = samples[-1]['node']
    if not start_node or not goal_node:
        raise ValueError('endpoint-packets-must-anchor-nodes')

    candidates, rejected_paths = [], []
    for path_no, path_edges in enumerate(enumerate_paths(network, start_node, goal_node), 1):
        total_m = path_distance(path_edges)
        edge_offsets, acc = [], 0
        for edge in path_edges:
            edge_offsets.append(acc)
            acc += int(edge['length_m'])
        anchor_lists = [
            _anchors_for_sample(sample, path_edges, edge_offsets, total_m)
            for sample in samples
        ]
        if any(not anchors for anchors in anchor_lists):
            rejected_paths.append({
                'edges': [e['id'] for e in path_edges],
                'reason': 'gps-ellipse-incompatible',
            })
            continue
        best = None
        for choice in product(*anchor_lists):
            feasible, checks = _interval_checks(samples, choice, vmax_ms)
            if feasible:
                best = (choice, checks)
                break
        if best is None:
            rejected_paths.append({
                'edges': [e['id'] for e in path_edges],
                'reason': 'time-or-speed-constraint-violated',
            })
            continue
        choice, checks = best
        path_roads = [e['road'] for e in path_edges if int(e['length_m']) > 0]
        required_signoffs = [{
            'type': 'station-arrival-handover',
            'at_node': goal_node,
            'signers': ['driver', 'station'],
        }]
        if set(path_roads) & {'B', 'C', 'D'}:
            required_signoffs.insert(0, {
                'type': 'b-route-stop-double-sign',
                'road': 'B',
                'signers': ['driver', 'station'],
            })
        # 端点原包锚定共享节点，其最近标签（如 B 路线两端的 A）不算未解释漂移；
        # 只有非节点样本的最近道路不在候选路径上，才需要相机/里程表漂移证据。
        unexplained = {
            s['nearest_road'] for s in samples
            if not s['node'] and s['nearest_road'] not in set(path_roads)
        }
        if unexplained:
            required_signoffs.append({
                'type': 'odometer-camera-drift-evidence',
                'unexplained_labels': sorted(unexplained),
                'signers': ['camera', 'vehicle'],
            })
        elapsed_h = (samples[-1]['ts'] - samples[0]['ts']) / 3600.0
        candidates.append({
            'candidate_id': f"{trip['trip_id']}#cand-{path_no}",
            'edge_ids': [e['id'] for e in path_edges],
            'roads': list(dict.fromkeys(path_roads)),
            'distance_m': total_m,
            'gps_compatible': True,
            'anchors': [
                {'packet_id': s['packet_id'], 'road': a[1], 'progress_m': a[0]}
                for s, a in zip(samples, choice)
            ],
            'interval_checks': checks,
            'departure_ts': samples[0]['ts'],
            'arrival_ts': samples[-1]['ts'],
            'required_speed_kmh': round(total_m / 1000.0 / elapsed_h, 3),
            'max_speed_kmh': vmax_kmh,
            'required_signoffs': required_signoffs,
        })
    candidates.sort(key=lambda c: (c['distance_m'], c['candidate_id']))
    return {
        'trip_id': trip['trip_id'],
        'raw_snap_labels': [s['nearest_road'] for s in samples],
        'raw_packet_ids': [s['packet_id'] for s in samples],
        'candidates': candidates,
        'rejected_packets': rejected,
        'rejected_paths': rejected_paths,
        'ambiguous': len(candidates) != 1,
    }


# ---------------------------------------------------------------------------
# 签认核验（司机/交接站双签、相机/里程表证据）
# ---------------------------------------------------------------------------

def _object_digest(obj, drop=('signatures',)):
    body = {k: v for k, v in obj.items() if k not in drop}
    return hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True,
                   separators=(',', ':')).encode('utf-8')
    ).hexdigest()


def verify_signatures(obj, signers, drop=('signatures',)):
    """核验对象上各签名方的 HMAC；signers 为角色→密钥。"""
    digest = _object_digest(obj, drop=drop)
    signatures = obj.get('signatures', {})
    for role, secret in signers.items():
        given = signatures.get(role)
        if not given or not hmac.compare_digest(given, sign_digest(digest, secret)):
            return False, f'{role}-signature-invalid', digest
    return True, 'verified', digest


def verify_camera_frame(frame, camera_secret):
    expected = hashlib.sha256(
        json.dumps({k: v for k, v in frame.items()
                    if k not in ('frame_sha256', 'camera_signature')},
                   ensure_ascii=False, sort_keys=True,
                   separators=(',', ':')).encode('utf-8')
    ).hexdigest()
    if frame.get('frame_sha256') != expected:
        return False, 'frame-digest-mismatch'
    if not hmac.compare_digest(frame.get('camera_signature', ''),
                               sign_digest(expected, camera_secret)):
        return False, 'camera-signature-invalid'
    return True, 'verified'


def _edge_offsets(network, candidate):
    edges_by_id = {e['id']: e for e in network['edges']}
    offsets, acc = [], 0
    for edge_id in candidate['edge_ids']:
        offsets.append((edges_by_id[edge_id], acc))
        acc += int(edges_by_id[edge_id]['length_m'])
    return offsets


def _stop_progress(network, candidate, stop):
    """停靠点在候选路径上的进度（米）；停靠道路不在路径上返回 None。"""
    for edge, offset in _edge_offsets(network, candidate):
        if edge['road'] == stop['road']:
            return offset + int(stop['along_m'])
    return None


def verify_stop(trip, stop, signer_secrets):
    """核验司机与交接站双签停靠凭据本身（不绑定候选路径）。"""
    ok, reason, digest = verify_signatures(stop, signer_secrets)
    if not ok:
        return {'stop_id': stop['stop_id'], 'verified': False, 'reason': reason}
    observed = int(stop['observed_ts'])
    start, end = int(stop['window_start_ts']), int(stop['window_end_ts'])
    if not start <= observed <= end:
        return {'stop_id': stop['stop_id'], 'verified': False,
                'reason': 'stop-outside-signed-window'}
    return {'stop_id': stop['stop_id'], 'verified': True,
            'reason': 'double-signed', 'digest': digest,
            'observed_ts': observed}


def _arrival_feasible(trip, candidate, arrival_ts):
    """从起点到终点节点，交接时刻是否满足该候选的时间/速度约束。"""
    vmax_ms = float(trip['max_speed_kmh']) / 3.6
    elapsed = int(arrival_ts) - int(trip['departure_ts'])
    if elapsed <= 0:
        return False
    return candidate['distance_m'] <= elapsed * vmax_ms + 1e-9


def _certificate(trip, candidate, basis, evidence_ids, raw_snap_labels, raw_packet_ids):
    return {
        'cert_id': f"{trip['trip_id']}-cert",
        'trip_id': trip['trip_id'],
        'version': 1,
        'basis': basis,
        'evidence_ids': list(evidence_ids),
        'edge_ids': list(candidate['edge_ids']),
        'roads': list(candidate['roads']),
        'distance_m': int(candidate['distance_m']),
        'departure_ts': int(trip['departure_ts']),
        'arrival_ts': candidate['arrival_ts'],
        'required_speed_kmh': candidate['required_speed_kmh'],
        'max_speed_kmh': candidate['max_speed_kmh'],
        'raw_snap_labels': list(raw_snap_labels),
        'raw_packet_ids': list(raw_packet_ids),
        'cert_digest': '',
    }


def certify_b_route(con, trip, report, stop, signer_secrets):
    """凭司机与交接站双签的 B 路停车凭据签发 B 路线（含 C/D）里程证书。"""
    stop_check = verify_stop(trip, stop, signer_secrets)
    if not stop_check['verified']:
        return {'certified': False, 'reason': stop_check['reason']}
    matches = [c for c in report['candidates'] if 'B' in c['roads']]
    if len(matches) != 1:
        return {'certified': False, 'reason': 'b-route-not-unique',
                'matching_candidates': len(matches)}
    candidate = matches[0]
    progress = _stop_progress(trip['network'], candidate, stop)
    if progress is None:
        return {'certified': False, 'reason': 'stop-road-not-on-candidate'}
    cert = _certificate(trip, candidate, 'b-route-double-signed-stop',
                        [stop['stop_id']], report['raw_snap_labels'],
                        report['raw_packet_ids'])
    cert['stop'] = {
        'stop_id': stop['stop_id'],
        'road': stop['road'],
        'progress_m': progress,
        'observed_ts': stop['observed_ts'],
        'digest': stop_check['digest'],
    }
    if con is not None:
        cert = s.save_route_certificate(con, cert)
    return {'certified': True, 'certificate': cert,
            'distance_m': cert['distance_m']}


def certify_direct_a(con, trip, report, evidence, secrets):
    """凭相机帧与车辆里程表证明中途 GPS 向 B 漂移，签发直行 A 里程证书。"""
    frame = evidence['camera_frame']
    ok, reason = verify_camera_frame(frame, secrets['camera'])
    if not ok:
        return {'certified': False, 'reason': reason}
    odo = evidence['odometer']
    odo_ok, odo_reason, odo_digest = verify_signatures(odo, {'vehicle': secrets['vehicle']})
    if not odo_ok:
        return {'certified': False, 'reason': odo_reason}
    matches = [c for c in report['candidates'] if c['roads'] == ['A']]
    if len(matches) != 1:
        return {'certified': False, 'reason': 'direct-a-not-unique',
                'matching_candidates': len(matches)}
    candidate = matches[0]
    travelled = int(odo['end_reading_m']) - int(odo['start_reading_m'])
    if abs(travelled - candidate['distance_m']) > int(evidence.get('tolerance_m', 50)):
        return {'certified': False,
                'reason': 'odometer-distance-mismatch',
                'odometer_m': travelled,
                'candidate_m': candidate['distance_m']}
    drift = frame.get('drift_to_road')
    if drift not in (None, 'B'):
        return {'certified': False, 'reason': 'camera-does-not-evidence-b-drift'}
    cert = _certificate(trip, candidate, 'odometer-camera-drift-evidence',
                        [frame['frame_id'], odo['odometer_id']],
                        report['raw_snap_labels'], report['raw_packet_ids'])
    cert['odometer'] = {'odometer_id': odo['odometer_id'],
                        'travelled_m': travelled, 'digest': odo_digest}
    cert['camera_frame'] = {'frame_id': frame['frame_id'], 'digest': frame['frame_sha256']}
    if con is not None:
        cert = s.save_route_certificate(con, cert)
    return {'certified': True, 'certificate': cert,
            'distance_m': cert['distance_m']}


def ambiguity_report(report):
    """证据不足以选路时的歧义报告：保留全部候选，不产生里程证书。"""
    return {
        'certified': False,
        'reason': 'multiple-candidate-routes-need-external-evidence',
        'candidate_ids': [c['candidate_id'] for c in report['candidates']],
        'candidate_distances_m': {c['candidate_id']: c['distance_m']
                                  for c in report['candidates']},
        'required_signoffs': {c['candidate_id']: c['required_signoffs']
                              for c in report['candidates']},
    }


# ---------------------------------------------------------------------------
# 到站交接核验与 service.pickup 对接
# ---------------------------------------------------------------------------

def register_handovers(con, trip, cert, handovers, candidates=None):
    """登记经核实的餐盒交接；只沿已签发证书的道路与时刻核验。

    B 路上未到站点的停靠若不属于当前证书，直接拒绝；站点交接还要满足
    该候选路径的时间/速度可行性。每个交接绑定餐盒与长者，供 pickup 认领。
    """
    if candidates is None:
        candidates = candidate_routes(trip)['candidates']
    cert_candidate = next(c for c in candidates if c['edge_ids'] == cert['edge_ids'])
    saved = []
    for handover in handovers:
        reason = None
        if handover['kind'] == 'mid-route-stop':
            on_route = any(road == handover['road'] for road in cert['roads'])
            if not on_route:
                reason = 'stop-road-not-on-certified-route'
        elif handover['kind'] == 'station-arrival':
            if not _arrival_feasible(trip, cert_candidate, handover['observed_ts']):
                reason = 'arrival-time-infeasible'
        else:
            reason = 'unknown-handover-kind'
        # 交接凭据只要求司机与交接站双方签名，相机/车辆密钥不参与该对象验签
        handover_signers = {role: secret for role, secret in
                            trip['signer_secrets'].items()
                            if role in ('driver', 'station')}
        ok, sig_reason, digest = verify_signatures(handover, handover_signers)
        if reason is None and not ok:
            reason = sig_reason
        row = {
            'handover_id': handover['handover_id'],
            'trip_id': trip['trip_id'],
            'cert_id': cert['cert_id'],
            'subject': handover['senior_id'],
            'box_id': handover['box_id'],
            'kind': handover['kind'],
            'road': handover.get('road'),
            'observed_ts': int(handover['observed_ts']),
            'state': 'verified' if reason is None else 'rejected',
            'reason': reason or 'handover-verified',
            'digest': digest,
        }
        saved.append(s.save_delivery_handover(con, row))
    return saved


def record_correction(con, trip, prior_cert_digest, kind, evidence, reason):
    """追加路线证书更正；原 GPS 原包、标签与司机签名永不抹去。"""
    return s.save_route_correction(con, {
        'correction_id': f"{trip['trip_id']}-corr-{s.next_correction_seq(con, trip['trip_id'])}",
        'trip_id': trip['trip_id'],
        'kind': kind,
        'prior_cert_digest': prior_cert_digest,
        'evidence_digest': _object_digest(evidence),
        'reason': reason,
        'evidence_ref': evidence.get('evidence_id', evidence.get('id', '')),
    })

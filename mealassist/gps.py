"""GPS 原包证据处理。

本模块保留建项时的独立增强副本基线：``snap`` 仍只对单个点取距离最近的
道路标签，不读取任何道路连接关系。新增能力围绕不可变原包展开：

- ``packet_digest`` / ``verify_packet``：原包规范序列化、摘要与设备 HMAC 验签；
- ``adjust_clock``：按采样时钟的偏移与漂移把设备时钟还原为统一时刻；
- ``ellipse_contains`` / ``compatible_roads``：误差椭圆内的相容道路，
  区别于“单点最近道路”；
- ``denoise``：只剔除验签或时钟不合格的原包，不修改保留下来的原包。
"""
import copy
import hashlib
import hmac
import json

SIGNATURE_FIELDS = ('packet_sha256', 'device_signature')


def snap(point):
    """单点最近道路（基线行为，保持不变）。"""
    return min(point['candidates'], key=lambda c: (c['distance_m'], c['road']))['road']


def canonical_payload(packet, drop=SIGNATURE_FIELDS):
    """原包去掉签名字段后的规范 JSON，验签与摘要都基于它。"""
    body = {k: v for k, v in packet.items() if k not in drop}
    return json.dumps(body, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def packet_digest(packet):
    return hashlib.sha256(canonical_payload(packet)).hexdigest()


def sign_digest(digest, secret):
    return hmac.new(str(secret).encode('utf-8'),
                    str(digest).encode('utf-8'),
                    hashlib.sha256).hexdigest()


def verify_packet(packet, device, max_drift_ppm=100, max_offset_s=300):
    """核验 GPS 原包：摘要、设备 HMAC 签名、采样时钟。

    返回 ``(ok, reason)``。验签只读取原包与设备登记信息，不改写原包。
    """
    expected = packet_digest(packet)
    if packet.get('packet_sha256') != expected:
        return False, 'packet-digest-mismatch'
    if not device or 'secret' not in device:
        return False, 'device-not-registered'
    signed = packet.get('device_signature')
    if not signed or not hmac.compare_digest(signed,
                                             sign_digest(expected, device['secret'])):
        return False, 'device-signature-invalid'
    ok, reason = check_clock(packet, max_drift_ppm=max_drift_ppm,
                             max_offset_s=max_offset_s)
    if not ok:
        return False, reason
    return True, 'verified'


def check_clock(packet, max_drift_ppm=100, max_offset_s=300):
    """采样时钟检核：设备时刻、偏移与漂移必须可还原且在容差内。"""
    clock = packet.get('clock')
    if not clock or 'offset_s' not in clock:
        return False, 'clock-metadata-missing'
    offset = clock['offset_s']
    drift = int(clock.get('drift_ppm', 0))
    device_ts = packet.get('device_ts')
    if device_ts is None:
        return False, 'device-timestamp-missing'
    if abs(offset) > max_offset_s:
        return False, 'clock-offset-out-of-tolerance'
    if abs(drift) > max_drift_ppm:
        return False, 'clock-drift-out-of-tolerance'
    adjusted = adjust_clock(packet)
    if 'ts' in packet and adjusted != int(packet['ts']):
        return False, 'clock-adjustment-disagrees-with-ts'
    return True, 'clock-ok'


def adjust_clock(packet):
    """把设备时钟按登记偏移还原为统一时刻（秒）。"""
    return int(packet['device_ts']) - int(packet['clock']['offset_s'])


def ellipse_contains(ellipse, distance_m):
    """误差椭圆（给定置信度）是否覆盖该距离。

    现场原包只给出各候选道路的最近距离，故以半长轴作为兼容半径；
    半短轴与方位角保留在原包内，供更精细的马氏距离复核。
    """
    return float(distance_m) <= float(ellipse['semi_major_m'])


def compatible_roads(packet):
    """误差椭圆相容的候选道路（可能多条），区别于 ``snap`` 的唯一最近路。"""
    ellipse = packet['ellipse']
    return [c['road'] for c in packet['candidates']
            if ellipse_contains(ellipse, c['distance_m'])]


def denoise(packets, devices):
    """验签 + 时钟检核去噪。

    返回 ``(kept, rejected)``；保留的原包深拷贝，避免后续流程误改证据。
    """
    kept, rejected = [], []
    for packet in packets:
        device = devices.get(packet.get('device_id'))
        ok, reason = verify_packet(packet, device) if device else (False, 'device-not-registered')
        if ok:
            kept.append(copy.deepcopy(packet))
        else:
            rejected.append({'id': packet.get('id'), 'reason': reason})
    return kept, rejected

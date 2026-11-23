"""语音订餐份额级谱系验收。

同一 C1/C2 原录音起点：
- 基线 capture 对 C1「午餐订2份」、C2「刚才2份中的1份转给乙」都只给首数字 2；
  逐段累加的常规补丁会汇总成 4 份，按号码归人只留最新数字的常规补丁
  会把 2 份都记在甲名下、乙得 0——两种都不是本系统执行的分支。
- 份额级谱系给出两条获准路径：乙截止前签字接受 → 甲1乙1总量2；
  乙拒绝后按甲已签「多出的一份退掉」→ 甲1乙0总量1，释放一份备餐库存。
"""
import json
from pathlib import Path
from mealassist import cli, storage, service
from mealassist import order_capture as oc

FIXTURE = Path(__file__).resolve().parent.parent / 'fixtures' / 'voice-orders.json'

C1 = {'segment': 'C1', 'speaker': 'senior-jia', 'start': 0.0, 'end': 4.2,
      'audio': 'sha256:a6afa0af3e394718f83efee1b9c6c0073e889770ff0512fcf48e4c624a15203d',
      'nbest': [{'text': '午餐订2份', 'score': 0.9}, {'text': '午餐订1份', 'score': 0.6}],
      'review': '午餐订2份', 'action': 'order', 'quantity': 2}
C2 = {'segment': 'C2', 'speaker': 'senior-jia', 'start': 30.5, 'end': 36.8,
      'audio': 'sha256:2640cf00e48209942c4473787ccfb971d8a78193f5f04ed5f44d085dd4ea73c9',
      'nbest': [{'text': '刚才2份中的1份转给乙', 'score': 0.87}, {'text': '刚才2份中的1份转给李', 'score': 0.41}],
      'review': '刚才2份中的1份转给乙', 'action': 'transfer', 'ref': 'C1',
      'recipient': 'senior-yi', 'quantity': 1, 'confirm_by': 80}


def fresh():
    return storage.connect(':memory:')


def enroll(con, sid, t):
    return cli.run_event(con, 'eligibility', {'id': f'enroll-{sid}', 'subject': sid, 'action': 'enroll',
                                              'data': {'monthly_limit': 30, 'version': 1}, 'effective_at': t, 'received_at': t})


def segment(con, event_id, t, **data):
    return cli.run_event(con, 'voice', {'id': event_id, 'subject': data.get('speaker', ''), 'action': 'segment',
                                        'data': data, 'effective_at': t, 'received_at': t})


def voice(con, event_id, subject, action, t, **data):
    return cli.run_event(con, 'voice', {'id': event_id, 'subject': subject, 'action': action,
                                        'data': data, 'effective_at': t, 'received_at': t})


def prepare(con, sid, event_id, orders, t, stock=5):
    return cli.run_event(con, 'service', {'id': event_id, 'subject': sid, 'action': 'prepare',
                                          'data': {'meal': 'lunch', 'stock': stock, 'orders': orders, 'source': 'voice-confirmed'},
                                          'effective_at': t, 'received_at': t})


def pickup(con, sid, event_id, prep, share, t):
    return cli.run_event(con, 'service', {'id': event_id, 'subject': sid, 'action': 'pickup',
                                          'data': {'prepare': prep, 'share': share}, 'effective_at': t, 'received_at': t})


def seed_c1c2(con):
    enroll(con, 'senior-jia', 1)
    enroll(con, 'senior-yi', 2)
    assert segment(con, 'seg-c1', 10, **C1)['outcome'] == 'ordered'
    assert segment(con, 'seg-c2', 20, **C2)['outcome'] == 'transfer-pending'


def test_capture_baseline_first_digit():
    # 单句副本基线：C1 得 2；C2 首数字也是 2，而有效数字其实是「1份转给乙」的 1
    assert oc.capture(C1['nbest']) == {'transcript': '午餐订2份', 'quantity': 2}
    assert oc.capture(C2['nbest']) == {'transcript': '刚才2份中的1份转给乙', 'quantity': 2}


def test_naive_patches_miscount():
    c1, c2 = oc.capture(C1['nbest']), oc.capture(C2['nbest'])
    # 常规错误一：逐段新增汇总成 4 份
    assert c1['quantity'] + c2['quantity'] == 4
    # 常规错误二：按号码归人只留最新数字，2 份都记在甲名下、乙得 0
    latest = {}
    for speaker, cap in (('senior-jia', c1), ('senior-jia', c2)):
        latest[speaker] = cap['quantity']
    assert latest == {'senior-jia': 2}
    assert 'senior-yi' not in latest


def test_structured_segment_stores_audio_model_review():
    con = fresh()
    segment(con, 'seg-c1', 10, **C1)
    row = storage.latest(con, 'segments', 'C1')
    assert row['speaker'] == 'senior-jia' and row['start'] == 0.0 and row['end'] == 4.2
    assert row['audio'].startswith('sha256:')          # 原录音
    assert row['transcript'] == '午餐订2份'              # 模型文字
    assert row['review'] == '午餐订2份'                  # 人工复核，三者并列保存
    payload = json.loads(row['payload'])
    assert [h['text'] for h in payload['nbest']] == ['午餐订2份', '午餐订1份']


def test_structured_capture_keeps_operative_vs_model_quantity():
    con = fresh()
    seed_c1c2(con)
    payload = json.loads(storage.latest(con, 'segments', 'C2')['payload'])
    assert payload['model_quantity'] == 2   # 首数字缺陷留痕
    assert payload['quantity'] == 1         # 人工复核确认的有效份额数
    assert payload['action'] == 'transfer' and payload['ref'] == 'C1' and payload['recipient'] == 'senior-yi'


def test_path_a_accept_transfer():
    con = fresh()
    seed_c1c2(con)
    assert oc.confirmed_orders(con) == {'senior-jia': 2}   # 未确认前乙没有份额
    r = voice(con, 'confirm-c2', 'senior-yi', 'confirm', 30, segment='C2', signature='yi-sign-20261123')
    assert r['outcome'] == 'confirmed'
    # 甲减1、乙加1、总量仍2：转赠不释放库存
    assert oc.confirmed_orders(con) == {'senior-jia': 1, 'senior-yi': 1}
    assert sum(oc.confirmed_orders(con).values()) == 2
    v = oc.verify_conservation(con)
    assert v['ok'] and v['live'] == 2 and v['per_order']['C1'] == {'created': 2, 'cancelled': 0, 'live': 2}
    # 只有经确认的逐人餐数汇入 prepare
    assert prepare(con, 'senior-jia', 'prep-jia', 1, 40)['outcome'] == 'prepared'
    assert prepare(con, 'senior-yi', 'prep-yi', 1, 41)['outcome'] == 'prepared'
    # 两个领取凭据各指向 C1 的不同份额
    assert pickup(con, 'senior-jia', 'pick-jia', 'prep-jia', 'C1#1', 50)['outcome'] == 'picked-up'
    assert pickup(con, 'senior-yi', 'pick-yi', 'prep-yi', 'C1#2', 51)['outcome'] == 'picked-up'
    creds = {storage.latest(con, 'documents', 'pick-jia')['state'],
             storage.latest(con, 'documents', 'pick-yi')['state']}
    assert creds == {'C1#1', 'C1#2'}
    owners = {s['id']: s['owner'] for s in oc.lineage_table(con)['shares']}
    assert owners == {'C1#1': 'senior-jia', 'C1#2': 'senior-yi'}   # 每份餐只有一个当前归属人
    hist = oc.lineage_table(con)['history']
    transfer = [h for h in hist if h['action'] == 'transfer']
    assert len(transfer) == 1 and transfer[0]['segment_id'] == 'C2' and transfer[0]['ref'] == 'C1'
    assert transfer[0]['share_id'] == 'C1#2'                        # 修正指向确切旧录音与旧份额
    assert service.commitment_drift(con) == {}
    assert cli.audit(con)['ok']


def test_path_b_reject_then_signed_cancel():
    con = fresh()
    seed_c1c2(con)
    r = voice(con, 'reject-c2', 'senior-yi', 'reject', 30, segment='C2')
    assert r['outcome'] == 'rejected'
    hist = oc.lineage_table(con)['history']
    # 两路均保留 C2 转赠尝试
    assert any(h['action'] == 'transfer' and h['segment_id'] == 'C2' and h['state'] == 'requested' for h in hist)
    assert any(h['action'] == 'reject' and h['segment_id'] == 'C2' for h in hist)
    assert oc.confirmed_orders(con) == {'senior-jia': 2}   # 拒绝后份额回到甲
    # 站点按长者甲另一句已签「多出的一份退掉」执行
    r = segment(con, 'seg-c3', 40, segment='C3', speaker='senior-jia', start=40.0, end=43.1,
                audio='sha256:29254ea5a3cfddc75d4ddb11ecd9a5fddea77635b205bba626bf64fe743cabbb',
                nbest=[{'text': '多出的一份退掉', 'score': 0.93}], review='多出的一份退掉',
                action='cancel', ref='C1', quantity=1, signed=True)
    assert r['outcome'] == 'cancelled'
    # 甲1乙0总量1：取消才释放库存，一份备餐库存被释放
    assert oc.confirmed_orders(con) == {'senior-jia': 1}
    v = oc.verify_conservation(con)
    assert v['ok'] and v['live'] == 1 and v['per_order']['C1'] == {'created': 2, 'cancelled': 1, 'live': 1}
    cancelled = [s['id'] for s in oc.lineage_table(con)['shares'] if s['state'] == 'cancelled']
    assert cancelled == ['C1#2']
    assert prepare(con, 'senior-jia', 'prep-jia', 1, 50)['outcome'] == 'prepared'
    assert service.commitment_drift(con) == {}
    assert cli.audit(con)['ok']


def test_same_share_cannot_be_transferred_twice():
    con = fresh()
    for i, sid in enumerate(('senior-jia', 'senior-yi', 'senior-bing', 'senior-ding'), 1):
        enroll(con, sid, i)
    # 单份额订单：唯一份额不可同时许给两人
    segment(con, 'seg-d1', 10, segment='D1', speaker='senior-jia', start=0.0, end=2.0, audio='sha256:d1',
            nbest=[{'text': '午餐订1份', 'score': 0.9}], action='order', quantity=1)
    assert segment(con, 'seg-t1', 11, segment='T1', speaker='senior-jia', start=3.0, end=5.0, audio='sha256:t1',
                   nbest=[{'text': '那1份转给乙', 'score': 0.9}], action='transfer', ref='D1',
                   recipient='senior-yi', quantity=1)['outcome'] == 'transfer-pending'
    r = segment(con, 'seg-t2', 12, segment='T2', speaker='senior-jia', start=6.0, end=8.0, audio='sha256:t2',
                nbest=[{'text': '那1份转给丙', 'score': 0.9}], action='transfer', ref='D1',
                recipient='senior-bing', quantity=1)
    assert r['outcome'] == 'blocked' and r['reason'] == 'insufficient-active-shares'
    voice(con, 'confirm-t1', 'senior-yi', 'confirm', 13, segment='T1', signature='yi-sign')
    r = segment(con, 'seg-t3', 14, segment='T3', speaker='senior-jia', start=9.0, end=11.0, audio='sha256:t3',
                nbest=[{'text': '那1份转给丁', 'score': 0.9}], action='transfer', ref='D1',
                recipient='senior-ding', quantity=1)
    assert r['outcome'] == 'blocked'   # 份额已归乙，甲无可转
    # 两份额订单：两个并行转赠锁定不同份额，第三个被拒
    segment(con, 'seg-c1', 20, **C1)
    segment(con, 'seg-t4', 21, segment='T4', speaker='senior-jia', start=12.0, end=14.0, audio='sha256:t4',
            nbest=[{'text': '转1份给丙', 'score': 0.9}], action='transfer', ref='C1',
            recipient='senior-bing', quantity=1)
    segment(con, 'seg-t5', 22, segment='T5', speaker='senior-jia', start=15.0, end=17.0, audio='sha256:t5',
            nbest=[{'text': '转1份给丁', 'score': 0.9}], action='transfer', ref='C1',
            recipient='senior-ding', quantity=1)
    r = segment(con, 'seg-t6', 23, segment='T6', speaker='senior-jia', start=18.0, end=20.0, audio='sha256:t6',
                nbest=[{'text': '再转1份给乙', 'score': 0.9}], action='transfer', ref='C1',
                recipient='senior-yi', quantity=1)
    assert r['outcome'] == 'blocked'
    locked = {h['share_id'] for h in oc.lineage_table(con)['history']
              if h['action'] == 'transfer' and h['segment_id'] in ('T4', 'T5')}
    assert locked == {'C1#1', 'C1#2'}   # 各锁一份，同一份额未被重复转出
    assert oc.verify_conservation(con)['ok']


def test_correction_without_ref_is_pending_not_latest_order():
    con = fresh()
    enroll(con, 'senior-jia', 1)
    segment(con, 'seg-c1', 10, **C1)
    # 「改成3份」没有明确所指订单：先悬挂，最后一句数字不能当最新订单
    r = segment(con, 'seg-c9', 20, segment='C9', speaker='senior-jia', start=50.0, end=53.0, audio='sha256:c9',
                nbest=[{'text': '刚才那单改成3份', 'score': 0.88}], action='correct', quantity=3)
    assert r['outcome'] == 'pending' and r['reason'] == 'no-explicit-ref'
    assert oc.confirmed_orders(con) == {'senior-jia': 2}
    assert [p['segment'] for p in oc.pending(con)] == ['C9']
    # 所指旧订单不存在同样悬挂
    r = segment(con, 'seg-c10', 21, segment='C10', speaker='senior-jia', start=54.0, end=56.0, audio='sha256:c10',
                nbest=[{'text': '退掉1份', 'score': 0.9}], action='cancel', ref='NOPE', quantity=1, signed=True)
    assert r['outcome'] == 'pending'
    # 人工复核补上确切所指后落单，拆分前后总餐数守恒
    r = voice(con, 'resolve-c9', 'staff-1', 'resolve', 30, segment='C9', ref='C1')
    assert r['outcome'] == 'corrected'
    assert oc.confirmed_orders(con) == {'senior-jia': 3}
    v = oc.verify_conservation(con)
    assert v['ok'] and v['per_order']['C1']['created'] == 3
    hist = oc.lineage_table(con)['history']
    assert any(h['action'] == 'correct' and h['segment_id'] == 'C9' and h['ref'] == 'C1' for h in hist)


def test_cancel_requires_signature():
    con = fresh()
    enroll(con, 'senior-jia', 1)
    segment(con, 'seg-c1', 10, **C1)
    r = segment(con, 'seg-c3', 20, segment='C3', speaker='senior-jia', start=40.0, end=43.0, audio='sha256:c3',
                nbest=[{'text': '多出的一份退掉', 'score': 0.9}], action='cancel', ref='C1', quantity=1)
    assert r['outcome'] == 'pending' and r['reason'] == 'awaiting-signature'
    assert oc.confirmed_orders(con) == {'senior-jia': 2}   # 未签不释放库存


def test_confirm_after_deadline_blocked():
    con = fresh()
    seed_c1c2(con)
    r = voice(con, 'confirm-late', 'senior-yi', 'confirm', 90, segment='C2', signature='yi-sign')
    assert r['outcome'] == 'blocked' and r['reason'] == 'confirmation-window-closed'
    assert oc.confirmed_orders(con) == {'senior-jia': 2}


def test_prepare_accepts_only_voice_confirmed_orders():
    con = fresh()
    enroll(con, 'senior-yi', 1)
    r = prepare(con, 'senior-yi', 'prep-yi', 1, 10)
    assert r['outcome'] == 'blocked' and r['reason'] == 'orders-not-voice-confirmed'
    # 不带 voice 来源的原有调用保持不变
    r = cli.run_event(con, 'service', {'id': 'prep-legacy', 'subject': 'senior-yi', 'action': 'prepare',
                                       'data': {'meal': 'lunch', 'stock': 2, 'orders': 1},
                                       'effective_at': 11, 'received_at': 11})
    assert r['outcome'] == 'prepared'


def test_late_correction_keeps_commitment_traceable():
    con = fresh()
    enroll(con, 'senior-jia', 1)
    segment(con, 'seg-c1', 10, **C1)
    assert prepare(con, 'senior-jia', 'prep-jia', 2, 20)['outcome'] == 'prepared'
    # 晚到的已签更正：甲退 1 份
    r = segment(con, 'seg-c4', 30, segment='C4', speaker='senior-jia', start=60.0, end=63.0, audio='sha256:c4',
                nbest=[{'text': '我退掉1份', 'score': 0.91}], review='我退掉1份',
                action='cancel', ref='C1', quantity=1, signed=True)
    assert r['outcome'] == 'cancelled'
    assert oc.confirmed_orders(con) == {'senior-jia': 1}
    # 已备餐承诺未被悄改，差异显式可追溯
    assert storage.latest(con, 'allocations', 'prep-jia')['state'] == 'prepared'
    assert service.commitment_drift(con) == {'senior-jia': {'prepared': 2, 'confirmed': 1}}
    # 下游显式更正：取消原承诺再按新确认数备餐
    cli.run_event(con, 'service', {'id': 'cancel-prep', 'subject': 'senior-jia', 'action': 'cancel',
                                   'data': {'prepare': 'prep-jia'}, 'effective_at': 40, 'received_at': 40})
    assert prepare(con, 'senior-jia', 'prep-jia-2', 1, 41)['outcome'] == 'prepared'
    assert service.commitment_drift(con) == {}
    assert cli.audit(con)['ok']


def test_fixture_voice_orders_path_a():
    rows = json.loads(FIXTURE.read_text(encoding='utf-8'))
    con = fresh()
    results = {r['event']: r for r in (cli.run_event(con, row['subsystem'], row['event']) for row in rows)}
    assert results['seg-c1']['outcome'] == 'ordered'
    assert results['seg-c2']['outcome'] == 'transfer-pending'
    assert results['confirm-c2-yi']['outcome'] == 'confirmed'
    assert results['prep-jia']['outcome'] == 'prepared' and results['prep-yi']['outcome'] == 'prepared'
    assert results['pick-jia']['outcome'] == 'picked-up' and results['pick-yi']['outcome'] == 'picked-up'
    assert oc.confirmed_orders(con) == {'senior-jia': 1, 'senior-yi': 1}
    creds = {storage.latest(con, 'documents', 'pick-jia')['state'],
             storage.latest(con, 'documents', 'pick-yi')['state']}
    assert creds == {'C1#1', 'C1#2'}
    assert oc.verify_conservation(con)['ok']
    assert service.commitment_drift(con) == {}
    assert cli.audit(con)['ok']

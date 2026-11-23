"""电话订餐录音的结构化采录与份额级订单谱系。

基线 capture() 只取最高分文字的第一个阿拉伯数字：C1「午餐订2份」得 2，
C2「刚才2份中的1份转给乙」仍得 2。逐段累加的常规补丁会汇总成 4 份；
按号码归人、只留最新数字的常规补丁会把 2 份都记在甲名下、乙得 0。
两种补丁都把每段话压成一个整数，无法表达 C2 的「甲减1、乙加1、总量仍2」。

本模块把每段录音采录为带时间轴、说话人、N-best、所指旧订单、
修正/分拆/撤销动作与被转赠人确认的结构化记录，并在 shares/lineage
两张表上维护份额级谱系：每份餐只有一个当前归属人，每次修正指向
确切旧录音与旧份额，拆分前后总餐数守恒，取消才释放库存；没有明确
所指订单的片段先悬挂，绝不把最后一句数字当最新订单。只有经确认的
逐人餐数（confirmed_orders）允许汇入 service.prepare 的 orders。
"""
import json, re
from .asr import top, nbest
from . import storage as s

NAME='语音订餐采录'
LIVE=('active','pending-out','picked-up')  # 未取消即仍占用备餐库存
CORRECTIONS=('transfer','cancel','correct')

def capture(hypotheses):
    """独立增强副本基线：top 文字的第一个阿拉伯数字作 quantity。"""
    text=top(hypotheses)
    match=re.search(r'\d+',text)
    return {'transcript':text,'quantity':int(match.group()) if match else 0}

def capture_segment(segment):
    """单段录音的结构化采录：模型文字、人工复核与原录音并列。"""
    hypotheses=segment.get('nbest') or [{'text':segment.get('text',''),'score':1.0}]
    model=capture(hypotheses)
    return {
        'segment':segment['segment'],
        'speaker':segment.get('speaker',''),
        'start':segment.get('start'),'end':segment.get('end'),
        'audio':segment.get('audio',''),
        'transcript':model['transcript'],
        'model_quantity':model['quantity'],
        'nbest':nbest(hypotheses),
        'review':segment.get('review'),
        'action':segment.get('action') or _suggest_action(model['transcript']),
        'ref':segment.get('ref'),
        'recipient':segment.get('recipient'),
        'quantity':segment.get('quantity'),
        'confirm_by':segment.get('confirm_by'),
        'signed':bool(segment.get('signed')),
    }

def _suggest_action(text):
    if '转给' in text or '转赠' in text: return 'transfer'
    if '退' in text or '取消' in text: return 'cancel'
    if '改' in text: return 'correct'
    return 'order'

def apply(c,e):
    a=e['action']
    if a=='segment': return _ingest_segment(c,e)
    if a=='confirm': return _confirm(c,e)
    if a=='reject': return _reject(c,e)
    if a=='resolve': return _resolve(c,e)
    return s.decide(c,e,NAME,'blocked','unknown-action','blocked')

def _ingest_segment(c,e):
    seg=capture_segment(e.get('data',{})); seg['event']=e['id']
    c.execute('INSERT OR REPLACE INTO segments VALUES(?,?,?,?,?,?,?,?)',
              (seg['segment'],seg['speaker'],seg['start'],seg['end'],seg['audio'],seg['transcript'],seg['review'],json.dumps(seg,ensure_ascii=False,sort_keys=True)))
    if seg['action']=='order':
        q=int(seg['quantity'] or seg['model_quantity'] or 0)
        if q<=0: return s.decide(c,e,NAME,'blocked','no-quantity','blocked')
        for i in range(1,q+1):
            share=f"{seg['segment']}#{i}"
            c.execute('INSERT INTO shares VALUES(?,?,?,?,?)',(share,seg['segment'],seg['segment'],seg['speaker'],'active'))
            _lineage(c,e,'order',seg,share,None,seg['speaker'],1,'confirmed','')
        return s.decide(c,e,NAME,'ordered',f'shares-created:{q}','confirmed')
    # 修正/分拆/撤销必须指向确切旧订单，否则悬挂，不把最后一句数字当最新订单
    if not seg.get('ref') or not _order_exists(c,seg['ref']):
        return s.decide(c,e,NAME,'pending','no-explicit-ref','pending')
    return _apply_action(c,e,seg)

def _apply_action(c,e,seg,note=''):
    act=seg.get('action')
    if act=='transfer':
        recipient=seg.get('recipient'); q=seg.get('quantity')
        if not recipient or not q: return s.decide(c,e,NAME,'pending','no-explicit-recipient-or-quantity','pending')
        shares=_owned_active(c,seg['ref'],seg['speaker'])
        if len(shares)<int(q): return s.decide(c,e,NAME,'blocked','insufficient-active-shares','blocked')
        for share in shares[:int(q)]:
            c.execute("UPDATE shares SET state='pending-out' WHERE id=?",(share,))
            _lineage(c,e,'transfer',seg,share,seg['speaker'],recipient,1,'requested',note or f"confirm_by:{seg.get('confirm_by')}")
        return s.decide(c,e,NAME,'transfer-pending',f'awaiting-recipient:{recipient}','pending')
    if act=='cancel':
        if not seg.get('signed'): return s.decide(c,e,NAME,'pending','awaiting-signature','pending')
        q=int(seg.get('quantity') or seg.get('model_quantity') or 0)
        if q<=0: return s.decide(c,e,NAME,'blocked','no-quantity','blocked')
        shares=_owned_active(c,seg['ref'],seg['speaker'])
        if len(shares)<q: return s.decide(c,e,NAME,'blocked','insufficient-active-shares','blocked')
        for share in shares[:q]:
            c.execute("UPDATE shares SET state='cancelled' WHERE id=?",(share,))
            _lineage(c,e,'cancel',seg,share,seg['speaker'],None,1,'confirmed',note or 'signed')
        return s.decide(c,e,NAME,'cancelled',f'shares-cancelled:{q}','confirmed')
    if act=='correct':
        q=seg.get('quantity')
        if q is None: return s.decide(c,e,NAME,'pending','no-explicit-quantity','pending')
        q=int(q); live=_live_count(c,seg['ref'],seg['speaker'])
        if q>live:
            top_idx=c.execute("SELECT COALESCE(MAX(CAST(SUBSTR(id,INSTR(id,'#')+1) AS INTEGER)),0) m FROM shares WHERE order_id=?",(seg['ref'],)).fetchone()['m']
            for i in range(top_idx+1,top_idx+1+(q-live)):
                share=f"{seg['ref']}#{i}"
                c.execute('INSERT INTO shares VALUES(?,?,?,?,?)',(share,seg['ref'],seg['segment'],seg['speaker'],'active'))
                _lineage(c,e,'correct',seg,share,None,seg['speaker'],1,'confirmed',note)
        elif q<live:
            shares=_owned_active(c,seg['ref'],seg['speaker'])
            if len(shares)<live-q: return s.decide(c,e,NAME,'blocked','insufficient-active-shares','blocked')
            for share in shares[:live-q]:
                c.execute("UPDATE shares SET state='cancelled' WHERE id=?",(share,))
                _lineage(c,e,'correct',seg,share,seg['speaker'],None,1,'confirmed',note)
        return s.decide(c,e,NAME,'corrected',f'own-shares:{live}->{q}','confirmed')
    return s.decide(c,e,NAME,'pending','unresolved-action','pending')

def _confirm(c,e):
    p=e.get('data',{}); sid=e['subject']; seg_id=p.get('segment')
    rows=_pending_transfers(c,seg_id,sid)
    if not rows: return s.decide(c,e,NAME,'blocked','no-pending-transfer','blocked')
    seg=s.latest(c,'segments',seg_id); payload=json.loads(seg['payload']) if seg else {}
    deadline=payload.get('confirm_by')
    if deadline is not None and int(e.get('effective_at',0))>int(deadline):
        return s.decide(c,e,NAME,'blocked','confirmation-window-closed','blocked')
    note=f"signature:{p.get('signature','')}"
    for r in rows:
        c.execute("UPDATE shares SET owner=?, state='active' WHERE id=?",(sid,r['share_id']))
        _lineage(c,e,'confirm',{'segment':seg_id,'ref':r['ref']},r['share_id'],r['from_owner'],sid,1,'confirmed',note)
    return s.decide(c,e,NAME,'confirmed',f'transfer-confirmed:{len(rows)}','confirmed')

def _reject(c,e):
    p=e.get('data',{}); sid=e['subject']; seg_id=p.get('segment')
    rows=_pending_transfers(c,seg_id,sid)
    if not rows: return s.decide(c,e,NAME,'blocked','no-pending-transfer','blocked')
    for r in rows:
        c.execute("UPDATE shares SET state='active' WHERE id=?",(r['share_id'],))
        _lineage(c,e,'reject',{'segment':seg_id,'ref':r['ref']},r['share_id'],r['from_owner'],sid,1,'rejected','transfer-declined')
    return s.decide(c,e,NAME,'rejected',f'transfer-declined:{len(rows)}','rejected')

def _resolve(c,e):
    p=e.get('data',{}); seg_id=p.get('segment'); ref=p.get('ref')
    seg=s.latest(c,'segments',seg_id)
    if not seg: return s.decide(c,e,NAME,'blocked','unknown-segment','blocked')
    payload=json.loads(seg['payload'])
    if payload.get('action') not in CORRECTIONS: return s.decide(c,e,NAME,'blocked','not-a-correction','blocked')
    if not ref or not _order_exists(c,ref): return s.decide(c,e,NAME,'pending','no-explicit-ref','pending')
    applied=c.execute("SELECT 1 FROM lineage WHERE segment_id=? AND action IN ('transfer','cancel','correct') LIMIT 1",(seg_id,)).fetchone()
    if applied: return s.decide(c,e,NAME,'blocked','segment-already-applied','blocked')
    payload['ref']=ref
    if p.get('quantity') is not None: payload['quantity']=p['quantity']
    return _apply_action(c,e,payload,note=f"resolved-by:{e['id']}")

def _order_exists(c,ref):
    return c.execute('SELECT 1 FROM shares WHERE order_id=? LIMIT 1',(ref,)).fetchone() is not None

def _owned_active(c,ref,owner):
    return [r['id'] for r in c.execute("SELECT id FROM shares WHERE order_id=? AND owner=? AND state='active' ORDER BY CAST(SUBSTR(id,INSTR(id,'#')+1) AS INTEGER) DESC",(ref,owner))]

def _live_count(c,ref,owner):
    return c.execute("SELECT COUNT(*) n FROM shares WHERE order_id=? AND owner=? AND state IN ('active','pending-out','picked-up')",(ref,owner)).fetchone()['n']

def _pending_transfers(c,segment,to_owner):
    return c.execute("""
        SELECT t.* FROM lineage t
        WHERE t.action='transfer' AND t.state='requested' AND t.segment_id=? AND t.to_owner=?
        AND NOT EXISTS (SELECT 1 FROM lineage r WHERE r.segment_id=t.segment_id AND r.share_id=t.share_id AND r.action IN ('confirm','reject'))
        ORDER BY t.seq""",(segment,to_owner)).fetchall()

def _lineage(c,e,action,seg,share,frm,to,qty,state,note):
    c.execute('INSERT INTO lineage(event_id,action,segment_id,ref,share_id,from_owner,to_owner,quantity,state,note) VALUES(?,?,?,?,?,?,?,?,?,?)',
              (e['id'],action,seg.get('segment'),seg.get('ref'),share,frm,to,qty,state,note))

def confirmed_orders(c):
    """经确认的逐人餐数：唯一允许汇入 service.prepare 的 orders。"""
    return {r['owner']:r['n'] for r in c.execute("SELECT owner,COUNT(*) n FROM shares WHERE state IN ('active','pending-out','picked-up') GROUP BY owner ORDER BY owner")}

def pending(c):
    """悬挂片段：修正/分拆/撤销已采录但未落单（缺所指订单或被拒）。"""
    out=[]
    for r in c.execute('SELECT id,payload FROM segments ORDER BY id'):
        p=json.loads(r['payload'])
        if p.get('action') in CORRECTIONS and not c.execute("SELECT 1 FROM lineage WHERE segment_id=? AND action IN ('transfer','cancel','correct') LIMIT 1",(r['id'],)).fetchone():
            out.append({'segment':r['id'],'action':p.get('action'),'speaker':p.get('speaker'),'ref':p.get('ref')})
    return out

def lineage_table(c):
    """订单谱系表：每份餐的当前归属人与全部历史。"""
    return {'shares':[dict(r) for r in c.execute('SELECT * FROM shares ORDER BY id')],
            'history':[dict(r) for r in c.execute('SELECT * FROM lineage ORDER BY seq')],
            'confirmed':confirmed_orders(c)}

def verify_conservation(c):
    """份额级守恒校验：创建=现存；未取消=确认合计；每次修正必有所指旧录音与旧份额。"""
    problems=[]
    for r in c.execute('SELECT * FROM lineage'):
        if not s.latest(c,'shares',r['share_id']): problems.append(f"missing-share:{r['share_id']}")
        if r['segment_id'] and not s.latest(c,'segments',r['segment_id']): problems.append(f"missing-segment:{r['segment_id']}")
        if r['ref'] and not _order_exists(c,r['ref']): problems.append(f"missing-ref:{r['ref']}")
    per_order={}
    for r in c.execute('SELECT order_id,state,COUNT(*) n FROM shares GROUP BY order_id,state'):
        v=per_order.setdefault(r['order_id'],{'created':0,'cancelled':0,'live':0})
        v['created']+=r['n']
        v['cancelled' if r['state']=='cancelled' else 'live']+=r['n']
    live=sum(v['live'] for v in per_order.values())
    if live!=sum(confirmed_orders(c).values()): problems.append('live-count-mismatch')
    return {'ok':not problems,'problems':problems,'live':live,'per_order':per_order}

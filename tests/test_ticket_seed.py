from mealassist.voucher import claim

def test_ocr_baseline():
    ticket={'ticket_id':'T1','ocr_candidates':[{'voucher_id':'A17','amount':10,'confidence':0.9},{'voucher_id':'A71','amount':20,'confidence':0.8}]}
    assert claim(ticket)=={'ticket_id':'T1','voucher_id':'A17','amount':10,'state':'accepted'}

def test_batch_reuses_top_voucher_without_uniqueness():
    t1={'ticket_id':'T1','ocr_candidates':[{'voucher_id':'A17','amount':10,'confidence':0.9},{'voucher_id':'A71','amount':20,'confidence':0.8}]}
    t2={'ticket_id':'T2','ocr_candidates':[{'voucher_id':'A17','amount':10,'confidence':0.95}]}
    assert claim(t1)['voucher_id']=='A17'
    assert claim(t2)['voucher_id']=='A17'

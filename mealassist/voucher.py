from .ticket_scan import choose

def claim(ticket):
    choice = choose(ticket['ocr_candidates'])
    return {'ticket_id': ticket['ticket_id'], 'voucher_id': choice['voucher_id'], 'amount': choice['amount'], 'state': 'accepted'}

"""语音转写假设：top 取最高分文字，nbest 保留完整候选供采录核对。"""

def top(hypotheses):
    return max(hypotheses, key=lambda x: x['score'])['text']

def nbest(hypotheses):
    """完整 N-best 列表（分数从高到低），与原录音、人工复核并列保存。"""
    return sorted(hypotheses, key=lambda x: x['score'], reverse=True)

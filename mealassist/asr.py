def top(hypotheses):
    return max(hypotheses, key=lambda x: x['score'])['text']

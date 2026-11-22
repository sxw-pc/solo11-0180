import re
from .asr import top

def capture(hypotheses):
    text=top(hypotheses)
    match=re.search(r'\d+',text)
    return {'transcript':text,'quantity':int(match.group()) if match else 0}

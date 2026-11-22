from mealassist.order_capture import capture

def test_simple_voice_order():
    result=capture([{'text':'午餐订2份','score':0.9},{'text':'午餐订1份','score':0.6}])
    assert result=={'transcript':'午餐订2份','quantity':2}

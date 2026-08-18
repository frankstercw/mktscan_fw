from mktscan.terminal import semantic_signal, iv_state, setup_quality

def test_semantic_states():
    assert semantic_signal(.6)=='STRONG BULL'
    assert semantic_signal(-.3)=='BEARISH'
    assert semantic_signal(.05)=='NEUTRAL'

def test_iv_states():
    assert iv_state(15)=='VERY LOW'
    assert iv_state(35)=='LOW'
    assert iv_state(85)=='VERY HIGH'

def test_setup_quality_surfaces_strengths_and_risks():
    q=setup_quality(.55,.8,'RISK_ON',None,25,3)
    assert q['label'] in {'MODERATE','HIGH'}
    assert any('directional' in x.lower() for x in q['strengths'])
    assert any('risk' in x.lower() for x in q['risks'])

from mktscan.analyst_ratings import analyst_momentum_from_events, score_analyst_event


def test_event_scoring_upgrade_and_pt_raise():
    event = {
        "action_company": "Upgrades",
        "action_pt": "Raises",
        "rating_current": "Buy",
    }
    assert score_analyst_event(event) == 3.0


def test_event_scoring_downgrade_and_pt_cut():
    event = {
        "action_company": "Downgrades",
        "action_pt": "Lowers",
        "rating_current": "Neutral",
    }
    assert score_analyst_event(event) == -3.0


def test_bullish_initiation():
    event = {
        "action_company": "Initiates",
        "action_pt": "",
        "rating_current": "Overweight",
    }
    assert score_analyst_event(event) == 1.5


def test_30_day_momentum_state_and_counts():
    events = [
        {"action_company": "Upgrades", "action_pt": "Raises", "rating_current": "Buy"},
        {"action_company": "Maintains", "action_pt": "Raises", "rating_current": "Buy"},
        {"action_company": "Downgrades", "action_pt": "", "rating_current": "Neutral"},
    ]
    result = analyst_momentum_from_events(events)
    assert result["score"] == 2.0
    assert result["state"] == "POSITIVE"
    assert result["events"] == 3
    assert result["upgrades"] == 1
    assert result["downgrades"] == 1
    assert result["pt_raises"] == 2
    assert result["pt_cuts"] == 0

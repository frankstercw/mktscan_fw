from bs4 import BeautifulSoup

from mktscan.analyst_ratings import analyst_momentum_from_events
from mktscan.scrapers.marketwatch import MarketWatchScraper


def test_marketwatch_current_calendar_schema():
    html = """
    <table>
      <tr><td>Friday, Aug. 21</td></tr>
      <tr>
        <td>9:45 AM</td>
        <td>US Flash Manufacturing PMI</td>
        <td>Aug.</td>
        <td>-</td>
        <td>54</td>
        <td>53.8</td>
      </tr>
      <tr><td>Add to My Calendar</td><td>PMI, Mfg</td></tr>
    </table>
    """
    scraper = MarketWatchScraper({"enabled": True}, delay=0)
    soup = BeautifulSoup(html, "lxml")
    rows = scraper._parse_calendar(soup)
    assert len(rows) == 1
    assert rows[0]["name"] == "US Flash Manufacturing PMI"
    assert rows[0]["consensus"] == "54"
    assert rows[0]["prior"] == "53.8"
    assert rows[0]["importance"] == "Medium"
    assert rows[0]["datetime"] is not None


def test_marketwatch_text_recovery_parser():
    html = """
    <table class="changed-wrapper">
      <tr><th>Wednesday, Aug. 26</th></tr>
      <tr>
        <td>8:30 AM</td><td>2nd estimate GDP</td><td>2Q</td>
        <td>-</td><td>-</td><td>1.5%</td>
      </tr>
    </table>
    """
    scraper = MarketWatchScraper({"enabled": True}, delay=0)
    rows = scraper._parse_calendar_text(BeautifulSoup(html, "lxml"))
    assert len(rows) == 1
    assert rows[0]["name"] == "2nd estimate GDP"
    assert rows[0]["importance"] == "High"


def test_analyst_momentum_yahoo_normalized_actions():
    events = [
        {"action_company": "Upgrades", "action_pt": "", "rating_current": "Buy"},
        {"action_company": "Downgrades", "action_pt": "", "rating_current": "Hold"},
    ]
    result = analyst_momentum_from_events(events)
    assert result["upgrades"] == 1
    assert result["downgrades"] == 1
    assert result["score"] == 0

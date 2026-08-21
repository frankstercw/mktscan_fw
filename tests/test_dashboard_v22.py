from bs4 import BeautifulSoup

from mktscan.on_demand import normalize_ticker
from mktscan.scrapers.marketwatch import MarketWatchScraper


def test_normalize_ad_hoc_ticker():
    assert normalize_ticker(" brk-b ") == "BRK-B"
    assert normalize_ticker("nvda") == "NVDA"


def test_marketwatch_current_calendar_columns_and_importance():
    html = """
    <table class="economic-calendar">
      <tr><th class="date">Friday, Aug. 21</th></tr>
      <tr>
        <td>9:45 AM</td>
        <td>US Flash Manufacturing PMI</td>
        <td>Aug.</td>
        <td>-</td>
        <td>54</td>
        <td>53.8</td>
      </tr>
      <tr>
        <td>8:30 AM</td>
        <td>Consumer Price Index CPI</td>
        <td>Aug.</td>
        <td>-</td>
        <td>0.3%</td>
        <td>0.2%</td>
      </tr>
    </table>
    """
    s = MarketWatchScraper({"enabled": True}, delay=0)
    rows = s._parse_calendar(BeautifulSoup(html, "lxml"))
    assert len(rows) == 2
    pmi = rows[0]
    assert pmi["consensus"] == "54"
    assert pmi["prior"] == "53.8"
    assert pmi["actual"] == "-"
    assert pmi["importance"] == "Medium"
    cpi = rows[1]
    assert cpi["importance"] == "High"
    # MarketWatch times are ET; persisted datetime is normalized to UTC.
    assert cpi["datetime"].hour in {12, 13}  # EDT/EST depending on inferred year/date

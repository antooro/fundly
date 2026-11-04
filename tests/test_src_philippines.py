"""Philippine UITFs (uitf.com.ph) against hand-made pages shaped like the real ones."""
import httpx

from fundly.sources import philippines
from fundly.sources.base import FUND, Inst
from fundly.sources.philippines import BANKS, UITFSource, _parse_bank_page
from test_sources import mock

MENU = '''<form name="dailynavpu" action="daily_navpu.php" class="mb-0">
<select name="bank_id" id="navpu-dropdown" onchange="form.submit();">
  <option value="">Check Daily NAVpu Values</option>
   <option value='3'> BPI WEALTH &nbsp;  <option value='6'> BDO Unibank, Inc. &nbsp; </select></form>'''


def row(fund_id, bank, name, cell):
    return f'''<tr>
      <td><a href="daily_navpu_details.php?fund_id={fund_id}&bank_id={bank}">{name}</a></td>
      <!-- <td></td> -->
      <td>{cell}</td>
      <td>-1.4046</td>
      <td>-5.2948</td>
    </tr>'''


def section(icon, title, rows):
    return f'''<div class="table-title mt-5">
      <img src="images/icons/{icon}.png" width="24" height="24" alt="">
      <h3 class="">{title}</h3>
    </div>
    <table><thead><tr><th>Fund Name</th><th>NAVpu</th><th>ROI-YOY%</th><th>ROI-YTD%</th></tr></thead>
    <tbody>{"".join(rows)}</tbody></table>'''


def page(day, *sections):
    heading = f'as of {day} <a href="#dateFilterModal" data-toggle="tooltip">' if day else 'as of  <a href="#dateFilterModal">'
    return f'''<h2>Unit Investment Trust Funds - Net Asset Values per unit (UITF NAVpus) {heading}</h2>
    <!-- <div class="table-title"><img src="images/icons/philippine-peso.png"><h3>Peso</h3></div> -->
    {"".join(sections)}'''


BPI = page("Nov 3, 2026",
           section("philippine-peso", "Equity Index Tracker Fund", [
               row(69, 3, "BPI PHILIPPINE EQUITY INDEX FUND", "83.53"),
               row(293, 3, "BPI PHILIPPINE CONSUMER EQUITY INDEX FUND", "0.00")]),
           section("philippine-peso", "Feeder Fund - Fixed Income", [
               row(558, 3, "BPI GLOBAL BOND INCOME FUND CLASS P (PHP CLASS)",
                   "1,110.33<br><span style='color:red'>* as of Nov 2, 2026</span>")]),
           section("dollar", "Feeder Fund - Equity", [row(296, 3, "BPI EUROPEAN EQUITY FEEDER FUND", "1.2345")]))
BDO = page(None, section("philippine-peso", "Money Market Fund", [row(901, 6, "BDO PESO MONEY MARKET FUND", "142.10")]))


def uitf(tmp_path=None, pages=None):
    pages = pages or {"3": BPI, "6": BDO}

    def handler(request):
        bank = request.url.params.get("bank_id")
        return httpx.Response(200, text=pages[bank] if bank else MENU)

    src = mock(UITFSource, [(lambda r: r.url.path == "/daily_navpu.php", handler)])
    src.files_dir = tmp_path
    return src


def bank_calls(src):
    return [r.url.params.get("bank_id", "menu") for r in src.calls]


def test_parse_reads_currency_category_and_each_rows_date():
    funds = _parse_bank_page(BPI, "3")
    assert funds["69"] == {"name": "BPI PHILIPPINE EQUITY INDEX FUND", "bank": "3", "currency": "PHP",
                           "category": "Equity Index Tracker Fund", "nav": 83.53, "date": "2026-11-03"}
    assert (funds["558"]["nav"], funds["558"]["date"]) == (1110.33, "2026-11-02")  # its own "as of", thousands comma
    assert funds["296"]["currency"] == "USD"
    assert funds["293"]["nav"] is None  # a zero NAVpu is no price
    assert _parse_bank_page(BDO, "6")["901"]["nav"] is None  # no date anywhere: refuse rather than guess


def test_quote_finds_the_bank_once_and_reuses_its_page():
    src = uitf()
    q = src.get_quote(Inst("ph-uitf:69", FUND))
    assert (q.nav, q.date, q.currency, q.source) == (83.53, "2026-11-03", "PHP", "uitf")
    assert src.get_quote(Inst("ph-uitf:296", FUND)).currency == "USD"
    assert bank_calls(src) == ["menu", "3", "6"]  # the catalogue reads every bank once; quotes reuse those pages
    assert src.get_quote(Inst("ph-uitf:293", FUND)) is None and src.get_quote(Inst("ph-uitf:12345", FUND)) is None


def test_the_catalogue_is_kept_on_disk_for_a_week(tmp_path):
    uitf(tmp_path).resolve("ph-uitf", "69")
    again = uitf(tmp_path)
    assert again.resolve("ph-uitf", "558") == {
        "id": "ph-uitf:558", "name": "BPI GLOBAL BOND INCOME FUND CLASS P (PHP CLASS)", "type": "fund",
        "currency": "PHP", "provider": "BPI WEALTH", "category": "Feeder Fund - Fixed Income"}
    assert again.calls == []


def test_history_is_what_this_machine_has_seen(tmp_path):
    uitf(tmp_path).get_quote(Inst("ph-uitf:69", FUND))
    later = page("Nov 4, 2026", section("philippine-peso", "Equity Index Tracker Fund", [
        row(69, 3, "BPI PHILIPPINE EQUITY INDEX FUND", "84.10")]))
    src = uitf(tmp_path, pages={"3": later, "6": BDO})
    h = src.get_history(Inst("ph-uitf:69", FUND), "2026-01-01", "2026-12-31")
    assert [(e.date, e.nav) for e in h.entries] == [("2026-11-03", 83.53), ("2026-11-04", 84.1)]
    assert (h.currency, h.source) == ("PHP", "uitf")


def test_without_a_cache_folder_history_is_the_latest_price():
    h = uitf().get_history(Inst("ph-uitf:69", FUND), "2026-01-01", "2026-12-31")
    assert [(e.date, e.nav) for e in h.entries] == [("2026-11-03", 83.53)]
    assert uitf().get_history(Inst("ph-uitf:69", FUND), "2020-01-01", "2020-12-31") is None


def test_search_builds_the_list_only_for_queries_meant_for_it(tmp_path):
    src = uitf(tmp_path)
    assert src.search("equity index") == [] and src.calls == []  # a fallback search costs nothing
    assert [h["id"] for h in src.search("bpi equity index")] == ["ph-uitf:69", "ph-uitf:293"]
    assert [h["id"] for h in src.search("bdo money market uitf")] == ["ph-uitf:901"]
    assert [h["id"] for h in src.search("european equity")] == ["ph-uitf:296"]  # list already there now


def test_bank_menu_falls_back_to_the_known_banks():
    src = mock(UITFSource, [(lambda r: True, httpx.Response(200, text="<html>maintenance</html>"))])
    assert src._banks() == BANKS


def test_is_slower_than_other_sources_but_not_robots_60_seconds():
    assert UITFSource.min_interval == 2.0 and philippines.CATALOGUE_TTL == 7 * 86400

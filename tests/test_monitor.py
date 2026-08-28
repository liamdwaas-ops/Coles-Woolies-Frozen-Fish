import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from openpyxl import load_workbook

from coles_monitor.changes import compare, visible_products
from coles_monitor.matcher import is_allowed_product, keyword_group, split_name_size
from coles_monitor.reporting import render_baseline_html, render_html, write_workbook
from coles_monitor.scraper import ColesScraper
from coles_monitor.woolworths import WoolworthsScraper
from run_monitor import scrape_with_fallback


def product(retailer, name, price=10.0):
    return {"retailer": retailer, "brand": "Example", "name": name, "price": price,
            "original_price": None, "promotional_price": None,
            "discount_percent": None, "availability_state": "in_stock",
            "availability_label": "Available", "size": "500g", "image_url": "",
            "online_only": False, "product_url": "https://example.test/product"}


class CategorySelectionTests(unittest.TestCase):
    def test_category_membership_not_keyword_matching_controls_inclusion(self):
        self.assertTrue(is_allowed_product("Ocean Chef Hoki Portions"))
        self.assertTrue(is_allowed_product("Salt & Pepper Calamari"))
        self.assertFalse(is_allowed_product(""))

    def test_report_groups_cover_every_category_sku(self):
        self.assertEqual(keyword_group("Raw Banana Prawns"), "Prawns & Shrimp")
        self.assertEqual(keyword_group("Salt & Pepper Squid"), "Squid & Calamari")
        self.assertEqual(keyword_group("Cooked Mussels"), "Shellfish")
        self.assertEqual(keyword_group("Crumbed Hoki"), "Fish & Other Seafood")

    def test_name_size(self):
        self.assertEqual(split_name_size("Crumbed Fish | 500g"), ("Crumbed Fish", "500g"))

    def test_coles_embedded_browse_results_are_found(self):
        payload = {"pageProps": {"searchResults": {"results": [
            {"id": 84452, "name": "Frozen Fish Fillets 500g", "brand": "Birds Eye",
             "pricing": {"now": 9.5}, "availability": True}
        ], "totalResults": 1}}}
        results, metadata = ColesScraper._find_results(payload)
        self.assertEqual(results[0]["id"], 84452)
        self.assertEqual(metadata["totalResults"], 1)

    def test_coles_multibuy_is_captured(self):
        _, mapped = ColesScraper._product({
            "id": 7, "name": "Frozen Hoki 500g", "availability": True,
            "pricing": {"now": 8.0, "offerDescription": "Pick any 2 for $12"},
        })
        self.assertEqual(mapped["original_price"], 8.0)
        self.assertEqual(mapped["promotional_price"], "Pick any 2 for $12")
        self.assertEqual(mapped["discount_percent"], 0.25)

    def test_woolworths_browse_bundles_are_found(self):
        payload = {"Bundles": [{"Products": [{"Stockcode": 502381,
                    "Name": "Frozen Prawns 500g", "Price": 12.0}]}]}
        products = WoolworthsScraper._find_products(payload)
        self.assertEqual(products[0]["Stockcode"], 502381)

    def test_woolworths_uses_exact_retailer_category_assignment(self):
        seafood = {"AdditionalAttributes": {
            "piescategorynamesjson": '["Frozen Seafood"]'}}
        chips = {"AdditionalAttributes": {
            "piescategorynamesjson": '["Frozen Chips & Wedges"]'}}
        self.assertTrue(WoolworthsScraper._is_frozen_seafood(seafood))
        self.assertFalse(WoolworthsScraper._is_frozen_seafood(chips))

    def test_woolworths_multibuy_is_captured_from_retailer_tag(self):
        _, mapped = WoolworthsScraper._product({
            "Stockcode": 8, "Name": "Frozen Prawns 500g", "Price": 10.0,
            "CentreTag": {"TagContent": "2 for $16",
                          "MultibuyData": {"MinQuantity": 2}},
            "IsAvailable": True, "IsInStock": True,
        })
        self.assertEqual(mapped["original_price"], 10.0)
        self.assertEqual(mapped["promotional_price"], "2 for $16")
        self.assertEqual(mapped["discount_percent"], 0.2)


class ChangeAndReportingTests(unittest.TestCase):
    def test_changed_fields_and_new_skus(self):
        old = {"coles:1": product("Coles", "Crumbed Hoki", 8.0)}
        new = {"coles:1": product("Coles", "Crumbed Hoki", 9.0),
               "woolworths:2": product("Woolworths", "Raw Prawns", 12.0)}
        events = compare(old, new, "2026-01-01T00:00:00+00:00")
        self.assertEqual([e["change_type"] for e in events], ["Price", "New"])
        self.assertIn("Raw Prawns", render_html(events))

    def test_workbook_has_retailer_sheets_and_category_sections(self):
        current = {"coles:1": product("Coles", "Crumbed Hoki"),
                   "woolworths:2": product("Woolworths", "Raw Prawns")}
        events = compare({}, current, "now")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "report.xlsx"
            write_workbook(path, events, current, report_events=events)
            workbook = load_workbook(path)
            self.assertEqual(workbook.sheetnames, ["Coles", "Woolworths", "Change History"])
            self.assertIn("Prawns & Shrimp", [cell.value for cell in workbook["Woolworths"]["A"]])
        self.assertIn("Crumbed Hoki", render_baseline_html(current))

    def test_temporary_unavailable_lifecycle(self):
        unavailable = {"1": {**product("Woolworths", "Frozen Squid"),
                              "availability_state": "temporary_unavailable",
                              "availability_label": "Temporarily unavailable"}}
        self.assertEqual(compare({}, unavailable, "now")[0]["change_type"], "Unavailable")
        self.assertEqual(compare(unavailable, unavailable, "later"), [])
        available = {"1": product("Woolworths", "Frozen Squid")}
        self.assertEqual(compare(unavailable, available, "later")[0]["change_type"], "Restocked")
        self.assertEqual(visible_products(unavailable, available), available)


class FallbackTests(unittest.TestCase):
    def test_failed_retailer_retains_last_verified_snapshot(self):
        class Failed:
            def scrape(self, url):
                raise RuntimeError("blocked")

        class Working:
            def scrape(self, url):
                return {"woolworths:2": product("Woolworths", "Frozen Prawns")}

        previous = {"coles:1": product("Coles", "Frozen Hoki")}
        current, failures = scrape_with_fallback(
            (("Coles", Failed()), ("Woolworths", Working())),
            {"Coles": "c", "Woolworths": "w"}, previous)
        self.assertIn("coles:1", current)
        self.assertIn("woolworths:2", current)
        self.assertEqual(len(failures), 1)


if __name__ == "__main__":
    unittest.main()

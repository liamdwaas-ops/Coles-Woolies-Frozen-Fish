import unittest

from coles_monitor.changes import compare, consolidate_events, visible_products
from coles_monitor.matcher import (category_group, is_allowed_product, is_wanted_name,
                                   keyword_group, split_name_size)
from coles_monitor.reporting import (email_visible_events, render_baseline_html,
                                     render_html, write_workbook)
from openpyxl import load_workbook
from pathlib import Path
from tempfile import TemporaryDirectory
from run_monitor import (configured_backup_scrapers, configured_scrapers, load_json,
                         reconcile_availability, scrape_with_fallback)
from coles_monitor.scraper import ColesScraper
from coles_monitor.woolworths import WoolworthsScraper


class MatcherTests(unittest.TestCase):
    def test_every_named_category_sku_is_wanted(self):
        self.assertTrue(is_wanted_name("Crumbed Hoki Fillets"))
        self.assertTrue(is_wanted_name("Raw Banana Prawns"))
        self.assertTrue(is_wanted_name("Salt & Pepper Calamari"))
        self.assertFalse(is_wanted_name(""))

    def test_page_membership_is_the_only_product_filter(self):
        self.assertTrue(is_allowed_product("Frozen Fish Fillets", "Birds Eye"))
        self.assertTrue(is_allowed_product("Seafood Marinara Mix", "Example"))
        self.assertTrue(is_allowed_product("Frozen Prawns", "Woolworths"))
        self.assertFalse(is_allowed_product("", "Example"))

    def test_name_size(self):
        self.assertEqual(split_name_size("Brand Pesto | 190g"), ("Brand Pesto", "190g"))

    def test_single_frozen_seafood_report_group(self):
        self.assertEqual(keyword_group("Crumbed Fish"), "Frozen Seafood")
        self.assertEqual(keyword_group("Prawn Dumplings"), "Frozen Seafood")
        self.assertIsNone(keyword_group(""))

    def test_retailer_taxonomy_does_not_exclude_a_category_sku(self):
        name = "Ocean Chef Hoki Portions"
        taxonomy = 'FREEZER - FISH ["Frozen Seafood", "Fish Fillets"]'
        self.assertEqual(category_group(name, taxonomy), "Frozen Seafood")
        self.assertTrue(is_allowed_product(name, "Ocean Chef", taxonomy))


class ReportingTests(unittest.TestCase):
    def test_retailer_and_keyword_sections_do_not_duplicate_skus(self):
        current = {
            "coles:1": {"retailer": "Coles", "brand": "A", "name": "Tomato Paste Passata",
                        "price": 2.0, "size": "100g", "image_url": "", "product_url": "https://example/1"},
            "woolworths:2": {"retailer": "Woolworths", "brand": "B", "name": "Pasta Sauce",
                             "price": 3.0, "size": "500g", "image_url": "", "product_url": "https://example/2"},
            "coles:unchanged": {"retailer": "Coles", "brand": "C", "name": "Basil Pesto",
                                "price": 4.0, "size": "190g", "image_url": "",
                                "product_url": "https://example/3"},
        }
        changed = {key: value for key, value in current.items() if key != "coles:unchanged"}
        report_events = compare({}, changed, "2026-01-01T00:00:00+00:00")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "report.xlsx"
            write_workbook(path, report_events, current, report_events=report_events)
            workbook = load_workbook(path)
            self.assertEqual(workbook.sheetnames, ["Coles", "Woolworths", "Change History"])
            self.assertNotIn("Image URL", [cell.value for cell in workbook["Coles"][4]])
            report_headers = [cell.value for cell in workbook["Coles"][4]]
            self.assertEqual(report_headers[2:5], ["Product", "Size", "Change Summary"])
            self.assertNotIn("Promotional Price (AUD)", report_headers)
            self.assertNotIn("Online Only", report_headers)
            history_headers = [cell.value for cell in workbook["Change History"][1]]
            self.assertNotIn("Before", history_headers)
            self.assertNotIn("After", history_headers)
            self.assertNotIn("Image URL", history_headers)
            self.assertNotIn("Promotional Price (AUD)", history_headers)
            self.assertNotIn("Online Only", history_headers)
            ids = []
            for sheet_name in ("Coles", "Woolworths"):
                ids.extend(cell.value for cell in workbook[sheet_name]["A"]
                           if isinstance(cell.value, str) and ":" in cell.value)
            self.assertCountEqual(ids, changed.keys())
            self.assertNotIn("coles:unchanged", ids)
        html = render_baseline_html(current)
        self.assertIn("<h2>Coles</h2>", html)
        self.assertIn("<h2>Woolworths</h2>", html)
        self.assertEqual(html.count(">Tomato Paste Passata</a>"), 1)
        self.assertNotIn("<th>Promotional Price</th>", html)
        self.assertNotIn("<th>Online Only</th>", html)
        self.assertIn("<th>Product</th><th>Size</th>", html)

    def test_email_orders_each_category_by_brand_and_marks_online_promotion(self):
        products = {
            "woolworths:1": {"retailer": "Woolworths", "brand": "Zulu",
                              "name": "Zulu Pasta Sauce", "size": "500g", "price": 4.0,
                              "product_url": "https://example/1", "availability_label": "Available"},
            "woolworths:2": {"retailer": "Woolworths", "brand": "Alpha",
                              "name": "Alpha Pasta Sauce", "size": "500g", "price": 3.0,
                              "original_price": 4.0, "promotional_price": 3.0,
                              "discount_percent": 0.25, "online_only": True,
                              "product_url": "https://example/2", "availability_label": "Available"},
        }
        html = render_baseline_html(products)
        self.assertLess(html.index("Alpha Pasta Sauce"), html.index("Zulu Pasta Sauce"))
        self.assertIn("$3.00 (Online only promotion)", html)

    def test_test_baseline_is_clearly_labelled(self):
        html = render_baseline_html({}, test=True)
        self.assertIn("Live test baseline", html)

    def test_failed_retailer_is_not_described_as_no_changes(self):
        html = render_html([], failures=["Coles: ScrapeError: blocked"])
        coles_section = html.split("<h2>Coles</h2>", 1)[1].split("<h2>Woolworths</h2>", 1)[0]
        self.assertIn("Refresh unavailable", coles_section)
        self.assertNotIn("No changes", coles_section)


class ScrapeFallbackTests(unittest.TestCase):
    def test_failed_retailer_retains_verified_snapshot(self):
        class FailedScraper:
            def scrape(self, queries):
                raise RuntimeError("blocked")

        class WorkingScraper:
            def scrape(self, queries):
                return {"woolworths:2": {"retailer": "Woolworths", "name": "New Passata"}}

        previous = {
            "coles:1": {"retailer": "Coles", "name": "Verified Tomato Paste"},
            "woolworths:1": {"retailer": "Woolworths", "name": "Old Passata"},
        }
        current, failures = scrape_with_fallback(
            (("Coles", FailedScraper()), ("Woolworths", WorkingScraper())), [], previous
        )
        self.assertIn("coles:1", current)
        self.assertNotIn("woolworths:1", current)
        self.assertIn("woolworths:2", current)
        self.assertEqual(len(failures), 1)


class TwoLocationAvailabilityTests(unittest.TestCase):
    @staticmethod
    def product(state, label=None):
        labels = {"in_stock": "Available", "temporary_unavailable": "Temporarily unavailable",
                  "out_of_stock": "Out of stock"}
        return {"retailer": "Coles", "name": "Frozen Hoki", "price": 8.0,
                "size": "500g", "product_url": "https://example.test/hoki",
                "availability_state": state,
                "availability_label": label or labels[state]}

    def test_matching_issue_in_both_suburbs_is_retained(self):
        primary = {"coles:1": self.product("temporary_unavailable")}
        backup = {"coles:1": self.product("temporary_unavailable")}
        result = reconcile_availability(primary, backup, {})
        self.assertEqual(result["coles:1"]["availability_state"],
                         "temporary_unavailable")

    def test_matching_no_availability_is_reported_once(self):
        previous = {"coles:1": self.product("in_stock")}
        primary = {"coles:1": self.product("out_of_stock")}
        backup = {"coles:1": self.product("out_of_stock")}
        result = reconcile_availability(primary, backup, previous)
        self.assertEqual(compare(previous, result, "now")[0]["change_type"],
                         "Unavailable")
        self.assertEqual(compare(result, result, "later"), [])

    def test_one_available_suburb_suppresses_new_issue(self):
        previous = {"coles:1": self.product("in_stock")}
        primary = {"coles:1": self.product("temporary_unavailable")}
        backup = {"coles:1": self.product("in_stock")}
        result = reconcile_availability(primary, backup, previous)
        self.assertEqual(result["coles:1"]["availability_state"], "in_stock")
        self.assertEqual(compare(previous, result, "now"), [])

    def test_different_issue_types_are_treated_as_no_change(self):
        previous = {"coles:1": self.product("temporary_unavailable")}
        primary = {"coles:1": self.product("temporary_unavailable")}
        backup = {"coles:1": self.product("out_of_stock")}
        result = reconcile_availability(primary, backup, previous)
        self.assertEqual(result["coles:1"]["availability_state"],
                         "temporary_unavailable")
        self.assertEqual(compare(previous, result, "now"), [])

    def test_restock_requires_both_suburbs_to_be_available(self):
        previous = {"coles:1": self.product("temporary_unavailable")}
        primary = {"coles:1": self.product("in_stock")}
        both_available = reconcile_availability(
            primary, {"coles:1": self.product("in_stock")}, previous)
        self.assertEqual(compare(previous, both_available, "now")[0]["change_type"],
                         "Restocked")

        disagreement = reconcile_availability(
            primary, {"coles:1": self.product("temporary_unavailable")}, previous)
        self.assertEqual(disagreement["coles:1"]["availability_state"],
                         "temporary_unavailable")
        self.assertEqual(compare(previous, disagreement, "later"), [])

    def test_backup_scrape_is_lazy_and_only_used_for_availability_verification(self):
        class StaticScraper:
            def __init__(self, products):
                self.products = products
                self.calls = 0

            def scrape(self, queries):
                self.calls += 1
                return self.products

        primary = StaticScraper({"coles:1": self.product("in_stock")})
        backup = StaticScraper({"coles:1": self.product("in_stock")})
        scrape_with_fallback((("Coles", primary),), [], {}, (("Coles", backup),))
        self.assertEqual(backup.calls, 0)

        primary.products = {"coles:1": self.product("temporary_unavailable")}
        scrape_with_fallback((("Coles", primary),), [], {}, (("Coles", backup),))
        self.assertEqual(backup.calls, 1)

    def test_broadway_backup_location_is_configured(self):
        config = load_json(Path(__file__).resolve().parents[1] / "config.json", {})
        backups = dict(configured_backup_scrapers(config))
        self.assertEqual(backups["Coles"].location["suburb"], "Broadway")
        self.assertEqual(backups["Coles"].location["postcode"], "2007")
        self.assertEqual(backups["Coles"]._resolve_store_id(), "839")
        self.assertEqual(backups["Woolworths"].location["state"], "NSW")


class LocationTests(unittest.TestCase):
    def test_configured_sources_are_exact_requested_pages(self):
        config = load_json(Path(__file__).resolve().parents[1] / "config.json", {})
        coles, woolworths = configured_scrapers(config)
        self.assertEqual(
            coles.category_url,
            "https://www.coles.com.au/browse/frozen/frozen-fish-seafood?sortBy=recommendedDescending",
        )
        self.assertEqual(
            woolworths.category_url,
            "https://www.woolworths.com.au/shop/browse/freezer/frozen-seafood",
        )

    def test_cheltenham_location_is_retained(self):
        location = {"suburb": "Cheltenham", "postcode": "3192", "state": "VIC",
                    "context_mode": "delivery"}
        scraper = ColesScraper(location=location)
        self.assertEqual(scraper.location, location)

    def test_coles_resolves_exact_cheltenham_fulfilment_store(self):
        scraper = ColesScraper(location={
            "suburb": "Cheltenham", "postcode": "3192", "state": "VIC"
        })

        def fake_api_get(path, params=None):
            if path.endswith("suggestions"):
                return {"localities": [{
                    "latitude": -37.96451, "longitude": 145.055873,
                    "postcode": "3192", "suburb": "Cheltenham", "state": "VIC",
                }]}
            return {"locations": [{
                "postcode": "3192", "distance": {"measurement": 0.77},
                "fulfillmentStore": {"storeId": "669"},
            }]}

        scraper._api_get = fake_api_get
        self.assertEqual(scraper._resolve_store_id(), "669")

    def test_coles_public_api_paginates_by_returned_page_size(self):
        scraper = ColesScraper(delay=0, max_pages=3, location={
            "suburb": "Cheltenham", "postcode": "3192", "state": "VIC"
        })
        scraper._resolve_store_id = lambda: "669"
        scraper._resolve_category = lambda store_id: {
            "id": "9373", "level": 2, "name": "Sauces"
        }
        starts = []

        def fake_api_get(path, params=None):
            starts.append(params["start"])
            page = params["start"]
            offset = page * 20
            count = 20 if page == 0 else 1
            return {
                "noOfResults": 21, "pageSize": 20,
                "results": [{
                    "id": offset + index + 1,
                    "name": f"Example Passata {offset + index + 1}",
                    "availability": True, "pricing": {"now": 3.0},
                } for index in range(count)],
            }

        scraper._api_get = fake_api_get
        self.assertEqual(len(scraper._browse_public_api()), 21)
        self.assertEqual(starts, [0, 1])

    def test_coles_multibuy_text_is_captured(self):
        _, product = ColesScraper._product({
            "id": "1", "name": "Example Passata", "availability": True,
            "pricing": {"now": 4.6, "specialType": "MULTI_SAVE",
                        "offerDescription": "Pick any 2 for $7",
                        "multiBuyPromotion": {"minQuantity": 2, "reward": 3.5}},
        })
        self.assertEqual(product["original_price"], 4.6)
        self.assertEqual(product["promotional_price"], "Pick any 2 for $7")
        self.assertEqual(product["discount_percent"], 0.2391)

    def test_coles_multibuy_outside_pricing_is_captured(self):
        _, product = ColesScraper._product({
            "id": "2", "name": "Example Pasta Sauce", "availability": True,
            "pricing": {"now": 4.5, "specialType": "MULTI_SAVE"},
            "promotions": [{"offerDescription": "Any 2 for $7"}],
        })
        self.assertEqual(product["original_price"], 4.5)
        self.assertEqual(product["promotional_price"], "2 for $7")
        self.assertEqual(product["discount_percent"], 0.2222)

    def test_coles_multibuy_badge_is_captured(self):
        _, product = ColesScraper._product({
            "id": "3", "name": "Example Pesto", "availability": True,
            "pricing": {"now": 6.0},
            "badges": {"promotion": {"PromotionText": "Buy 2 for $10.00"}},
        })
        self.assertEqual(product["promotional_price"], "2 for $10.00")
        self.assertEqual(product["discount_percent"], 0.1667)

    def test_coles_ordered_images_are_retained(self):
        _, product = ColesScraper._product({
            "id": "4", "name": "Example Passata", "availability": True,
            "pricing": {"now": 3.0},
            "imageUris": [{"uri": "/4/4.jpg"}, {"uri": "/4/4_2.jpg"}],
        })
        self.assertEqual(product["image_urls"], [
            "https://cdn.productimages.coles.com.au/productimages/4/4.jpg",
            "https://cdn.productimages.coles.com.au/productimages/4/4_2.jpg",
        ])

    def test_coles_taxonomy_classifies_stir_through_as_pasta_sauce(self):
        _, product = ColesScraper._product({
            "id": "5", "name": "Roasted Vegetables Stir Through Sauce",
            "brand": "Leggo's", "availability": True, "pricing": {"now": 4.6},
            "merchandiseHeir": {
                "category": "MEAL BASES", "subCategory": "PASTA SAUCE",
                "className": "CHUNKY",
            },
            "onlineHeirs": [{"aisle": "Pizza & Pasta"}],
        })
        self.assertEqual(product["category_group"], "Frozen Seafood")


class WoolworthsTests(unittest.TestCase):
    def test_nested_search_response_mapping(self):
        payload = {"Products": [{"Products": [{
            "Stockcode": 502381,
            "Name": "Woolworths Passata 680g",
            "PackageSize": "680g",
            "Price": 2.25,
            "MediumImageFile": "https://cdn.example.test/502381.jpg",
            "UrlFriendlyName": "woolworths-passata"
        }]}]}
        products = WoolworthsScraper._find_products(payload)
        self.assertEqual(len(products), 1)
        product_id, product = WoolworthsScraper._product(products[0])
        self.assertEqual(product_id, "woolworths:502381")
        self.assertEqual(product["retailer"], "Woolworths")
        self.assertEqual(product["name"], "Woolworths Passata")
        self.assertEqual(product["size"], "680g")
        self.assertEqual(product["price"], 2.25)
        self.assertFalse(product["online_only"])

    def test_woolworths_taxonomy_and_ordered_images_are_retained(self):
        _, product = WoolworthsScraper._product({
            "Stockcode": 957033,
            "Name": "Leggo's Stir Through Tomato Garlic & Caramelised Onion Sauce",
            "Brand": "Leggo's", "Price": 4.3, "PackageSize": "350g",
            "MediumImageFile": "https://cdn.example/medium/957033.jpg",
            "AdditionalAttributes": {
                "sapsubcategoryname": "PASTA SAUCE & CHEESE",
                "sapsegmentname": "PASTA SAUCE STIR THRU",
                "productimages": "957033.jpg,957033_2.jpg",
            },
        })
        self.assertEqual(product["category_group"], "Frozen Seafood")
        self.assertEqual(product["image_urls"], [
            "https://cdn.example/medium/957033.jpg",
            "https://cdn.example/medium/957033_2.jpg",
        ])

    def test_woolworths_online_only_flag(self):
        _, product = WoolworthsScraper._product({
            "Stockcode": 99, "Name": "Example Pesto 190g", "PackageSize": "190g",
            "Price": 4.0, "IsOnlineOnly": True
        })
        self.assertTrue(product["online_only"])

    def test_woolworths_promotion_fields_require_explicit_promo(self):
        _, promo = WoolworthsScraper._product({
            "Stockcode": 1, "Name": "Example Passata 700g", "PackageSize": "700g",
            "Brand": "Example", "Price": 3.0, "WasPrice": 4.0, "IsOnSpecial": True,
            "IsAvailable": True, "IsInStock": True
        })
        self.assertEqual(promo["original_price"], 4.0)
        self.assertEqual(promo["promotional_price"], 3.0)
        self.assertEqual(promo["discount_percent"], 0.25)
        _, not_promo = WoolworthsScraper._product({
            "Stockcode": 2, "Name": "Example Passata 700g", "PackageSize": "700g",
            "Brand": "Example", "Price": 3.0, "WasPrice": 4.0, "IsOnSpecial": False
        })
        self.assertIsNone(not_promo["original_price"])

    def test_woolworths_multibuy_text_is_captured(self):
        _, product = WoolworthsScraper._product({
            "Stockcode": 5, "Name": "Example Passata", "Price": 4.0,
            "PromotionDescription": "2 for $6", "IsAvailable": True, "IsInStock": True,
        })
        self.assertEqual(product["original_price"], 4.0)
        self.assertEqual(product["promotional_price"], "2 for $6")
        self.assertEqual(product["discount_percent"], 0.25)
        old = {"woolworths:5": {**product, "promotional_price": None,
                                 "original_price": None, "discount_percent": None}}
        events = compare(old, {"woolworths:5": product}, "now")
        self.assertEqual(events[0]["change_type"], "Promotion")
        self.assertIn("2 for $6", render_html(events))

    def test_woolworths_availability_mapping(self):
        _, temporary = WoolworthsScraper._product({
            "Stockcode": 3, "Name": "Example Passata 700g",
            "IsAvailable": False, "IsInStock": False
        })
        self.assertEqual(temporary["availability_state"], "temporary_unavailable")
        _, out = WoolworthsScraper._product({
            "Stockcode": 4, "Name": "Example Passata 700g",
            "IsAvailable": True, "IsInStock": False
        })
        self.assertEqual(out["availability_state"], "out_of_stock")


class OnlineOnlyChangeTests(unittest.TestCase):
    def test_status_change_is_reported(self):
        old = {"coles:1": {"name": "A Pesto", "price": 2.0, "size": "100g",
                           "image_url": "a", "online_only": False}}
        new = {"coles:1": {"retailer": "Coles", "name": "A Pesto", "price": 2.0,
                           "size": "100g", "image_url": "a", "online_only": True,
                           "product_url": "u"}}
        events = compare(old, new, "2026-01-01T00:00:00+00:00")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["change_type"], "Online only")
        self.assertTrue(events[0]["online_only"])


class EmailVisibilityTests(unittest.TestCase):
    def test_promotion_ending_is_retained_but_hidden_from_email(self):
        old = {"coles:1": {"retailer": "Coles", "name": "Example Passata",
                            "price": 3.0, "original_price": 4.0,
                            "promotional_price": 3.0, "discount_percent": 0.25,
                            "size": "700g", "image_url": "", "product_url": "u"}}
        new = {"coles:1": {**old["coles:1"], "price": 4.0,
                            "original_price": None, "promotional_price": None,
                            "discount_percent": None}}
        events = compare(old, new, "now")
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["promotion_ended"])
        self.assertEqual(email_visible_events(events), [])
        self.assertNotIn("Example Passata", render_html(events))


class AvailabilityLifecycleTests(unittest.TestCase):
    def test_temporary_unavailable_once_then_back_in_stock(self):
        temporary = {"1": {"retailer": "Woolworths", "name": "A Passata",
                            "availability_state": "temporary_unavailable",
                            "availability_label": "Temporarily unavailable",
                            "product_url": "u"}}
        first = compare({}, temporary, "now")
        self.assertEqual(first[0]["change_type"], "Unavailable")
        self.assertEqual(compare(temporary, temporary, "later"), [])
        self.assertEqual(visible_products({}, temporary), temporary)
        self.assertEqual(visible_products(temporary, temporary), {})
        available = {"1": {**temporary["1"], "availability_state": "in_stock",
                           "availability_label": "Available"}}
        back = compare(temporary, available, "later")
        self.assertEqual(back[0]["change_type"], "Restocked")
        self.assertEqual(visible_products(temporary, available), available)

    def test_out_of_stock_is_reported_once_then_suppressed(self):
        out = {"1": {"name": "A Passata", "availability_state": "out_of_stock"}}
        self.assertEqual(compare({}, out, "now")[0]["change_type"], "Unavailable")
        self.assertEqual(compare(out, out, "later"), [])
        self.assertEqual(visible_products({}, out), out)


class ChangeTests(unittest.TestCase):
    def test_changed_fields_new_products_and_deduplication(self):
        old = {"1": {"name": "A Pesto", "price": 2.0, "size": "100g", "image_url": "a"}}
        new = {
            "1": {"name": "A Pesto", "price": 2.5, "size": "100g", "image_url": "b", "product_url": "u"},
            "2": {"name": "B Passata", "price": 3.0, "size": "700g", "image_url": "c", "product_url": "v"},
        }
        events = compare(old, new, "2026-01-01T00:00:00+00:00")
        self.assertEqual([e["change_type"] for e in events],
                         ["RRP changed; Image 1 changed", "New"])
        self.assertEqual(len({e["product_id"] for e in events}), len(events))
        self.assertEqual(compare(old, new, "later", [e["event_id"] for e in events]), [])

    def test_price_summaries_distinguish_rrp_and_promotion(self):
        base = {"name": "A Pesto", "size": "100g", "image_url": "a",
                "product_url": "u"}
        rrp = compare({"1": {**base, "price": 4.0}},
                      {"1": {**base, "price": 5.0}}, "now")
        self.assertEqual(rrp[0]["change_type"], "RRP changed")
        promotion = compare(
            {"1": {**base, "price": 4.0, "original_price": None,
                    "promotional_price": None, "discount_percent": None}},
            {"1": {**base, "price": 3.0, "original_price": 4.0,
                    "promotional_price": 3.0, "discount_percent": 0.25}}, "later")
        self.assertEqual(promotion[0]["change_type"], "Promotion")

    def test_image_change_names_the_positions(self):
        base = {"name": "A Pesto", "price": 4.0, "size": "100g", "product_url": "u"}
        old = {"1": {**base, "image_url": "a", "image_urls": ["a", "b", "c"]}}
        new = {"1": {**base, "image_url": "a", "image_urls": ["a", "d", "c", "e"]}}
        events = compare(old, new, "now")
        self.assertEqual(events[0]["change_type"], "Image 2 changed; Image 4 added")

    def test_legacy_history_is_consolidated_per_sku_and_observation(self):
        events = [
            {"observed_at": "now", "product_id": "coles:1", "change_type": "Price changed",
             "event_id": "a", "name": "Pesto"},
            {"observed_at": "now", "product_id": "coles:1", "change_type": "Image changed",
             "event_id": "b", "name": "Pesto"},
        ]
        consolidated = consolidate_events(events)
        self.assertEqual(len(consolidated), 1)
        self.assertEqual(consolidated[0]["change_type"], "Price; Image")


if __name__ == "__main__":
    unittest.main()

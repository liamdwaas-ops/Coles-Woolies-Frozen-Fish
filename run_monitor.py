import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from coles_monitor.changes import compare, consolidate_events, visible_products
from coles_monitor.matcher import is_allowed_product
from coles_monitor.promotions import find_multibuy_text
from coles_monitor.reporting import email_visible_events, send_email, write_workbook
from coles_monitor.scraper import ColesScraper
from coles_monitor.woolworths import WoolworthsScraper


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"


def scrape_with_fallback(scrapers, category_pages, previous):
    """Scrape retailers independently, retaining last verified data on access failures."""
    current = {}
    failures = []
    for retailer, scraper in scrapers:
        try:
            current.update(scraper.scrape(category_pages[retailer]))
        except Exception as exc:
            retained = {product_id: product for product_id, product in previous.items()
                        if product.get("retailer") == retailer}
            if not retained:
                raise
            current.update(retained)
            failures.append(f"{retailer}: {type(exc).__name__}: {exc}")
    for failure in failures:
        print(f"::warning title=Retailer snapshot retained::{failure}", file=sys.stderr)
    return current, failures


def load_json(path, default):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-email", action="store_true")
    parser.add_argument("--email-baseline", action="store_true")
    parser.add_argument("--send-existing-baseline", action="store_true")
    parser.add_argument("--send-latest-events", action="store_true")
    parser.add_argument("--send-multibuy-test", action="store_true")
    parser.add_argument("--reset-baseline", action="store_true",
                        help="Adopt the current catalogue without recording schema/filter changes")
    parser.add_argument("--fixture", help="Use a local JSON product snapshot (tests only)")
    args = parser.parse_args()
    config = load_json(ROOT / "config.json", {})
    previous = {product_id: product for product_id, product in
                load_json(DATA / "current.json", {}).items()
                if is_allowed_product(product.get("name", ""), product.get("brand", ""))}
    history = consolidate_events(load_json(DATA / "events.json", []))
    workbook_path = DATA / "coles-woolworths-frozen-seafood-change-history.xlsx"
    if args.send_multibuy_test:
        test_events = []
        observed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        for product_id, product in previous.items():
            if find_multibuy_text(product.get("promotional_price", "")):
                test_events.append({**product, "product_id": product_id,
                                    "observed_at": observed_at,
                                    "change_type": "Multibuy"})
        if not test_events:
            raise RuntimeError("No explicit multibuy offers are present in the current snapshot")
        write_workbook(workbook_path, history, previous, report_events=test_events)
        password = os.environ.get("GMAIL_APP_PASSWORD", "")
        if not password:
            raise RuntimeError("GMAIL_APP_PASSWORD is required to send the multibuy test")
        send_email(config["sender"], config["recipient"], password, test_events,
                   workbook_path)
        print(json.dumps({"multibuy_test_products": len(test_events)}))
        return
    if args.send_latest_events:
        if not history or not workbook_path.exists():
            raise RuntimeError("Existing change history and workbook are required")
        latest_observed_at = max(event["observed_at"] for event in history)
        latest_events = [event for event in history
                         if event["observed_at"] == latest_observed_at]
        write_workbook(workbook_path, history, previous, report_events=latest_events)
        password = os.environ.get("GMAIL_APP_PASSWORD", "")
        if not password:
            raise RuntimeError("GMAIL_APP_PASSWORD is required to resend the latest update")
        send_email(config["sender"], config["recipient"], password, latest_events,
                   workbook_path)
        print(json.dumps({"resent_events": len(latest_events),
                          "observed_at": latest_observed_at}))
        return
    if args.send_existing_baseline:
        if not previous or not workbook_path.exists():
            raise RuntimeError("An existing baseline snapshot and workbook are required")
        password = os.environ.get("GMAIL_APP_PASSWORD", "")
        if not password:
            raise RuntimeError("GMAIL_APP_PASSWORD is required to email the baseline")
        send_email(config["sender"], config["recipient"], password, [], workbook_path,
                   baseline=previous)
        print(json.dumps({"baseline_email_products": len(previous)}))
        return
    if args.fixture:
        current = load_json(Path(args.fixture), {})
    else:
        coles = ColesScraper(
            config["request_delay_seconds"], config["max_pages"],
            config["page_size"], config.get("location"),
            config.get("coles_verified_build_id_fallback", "")
        )
        woolworths = WoolworthsScraper(
            config["request_delay_seconds"], config["max_pages"],
            config["page_size"], config.get("location"),
            config.get("woolworths_category")
        )
        current, scrape_failures = scrape_with_fallback(
            (("Coles", coles), ("Woolworths", woolworths)),
            config["category_pages"], previous
        )
    current = {product_id: product for product_id, product in current.items()
               if is_allowed_product(product.get("name", ""), product.get("brand", ""))}
    observed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    first_run = not previous
    display_current = visible_products(previous, current, first_run or args.reset_baseline)
    events = [] if first_run or args.reset_baseline else compare(
        previous, current, observed_at, (e["event_id"] for e in history)
    )
    updated_history = history + events
    write_workbook(workbook_path, updated_history, display_current, report_events=events)
    save_json(DATA / "current.json", current)
    save_json(DATA / "events.json", updated_history)
    print(json.dumps({"products": len(current), "changes": len(events), "baseline": first_run,
                      "retailer_failures": scrape_failures if not args.fixture else []}))
    if args.no_email:
        return
    password = os.environ.get("GMAIL_APP_PASSWORD", "")
    body_events = email_visible_events(events)
    should_email = ((first_run and args.email_baseline) or
                    (not first_run and bool(body_events)))
    if not should_email:
        return
    if not password:
        raise RuntimeError("GMAIL_APP_PASSWORD is required when changes need to be emailed")
    send_email(config["sender"], config["recipient"], password, body_events, workbook_path,
               baseline=display_current if first_run else None)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        message = str(exc).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error title=Monitor failure::{type(exc).__name__}: {message}", file=sys.stderr)
        raise

# Coles and Woolworths frozen seafood product change monitor

This repository checks these retailer category pages once a week:

- Coles: `https://www.coles.com.au/browse/frozen/frozen-fish-seafood?sortBy=recommendedDescending`
- Woolworths: `https://www.woolworths.com.au/shop/browse/freezer/frozen-seafood`

Every page is retrieved and every named SKU returned by the configured retailer category is included under **Frozen Seafood**. Product-title keywords and brand exclusions are not used; membership of the two supplied category pages is the selection rule.

It records product-name, current-price, pack-size, ordered product-image and **Online Only** status changes, plus newly listed matching products. Image changes identify the affected retailer image position, such as `Image 2 changed` or `Image 4 added`. A new flavour with a new SKU is reported as **New**; a flavour rename on an existing SKU is reported as **Name**. This avoids guessing whether marketing text represents a flavour. Online-only promotions remain in the report and are labelled in the `Current Price` cell.

Each weekly email and the main Coles/Woolworths workbook sheets contain only SKUs that changed versus the previous successful weekly snapshot, with one row per changed SKU. Unchanged catalogue SKUs are omitted. Rows within each category are ordered by brand and then product name. The `Size` column sits immediately after the linked product name. A compact `Change Summary` distinguishes `RRP changed` from `Promotion` and combines simultaneous changes with labels such as `New`, `Image 2 changed`, `Unavailable`, or `Restocked`. Before, after and image columns are intentionally omitted; the separate `Change History` sheet remains the audit trail across runs.

When a SKU was promotional in the previous weekly snapshot and has returned to full price, that SKU is retained in the workbook audit trail but omitted from the email body. If a run contains only promotion-ending changes, no email is sent.

Current price, original price and percentage discount have separate columns. There is no redundant promotional-price or online-only column. Standard promotional prices already appear as `Current Price`; an online-only promotion is labelled there.

Explicit multibuy offers such as `2 for $14.00` are recorded verbatim in the `Current Price` column beside the single-item price. The discount percentage is calculated from the retailer-provided multibuy quantity and total against the current single-item price; no multibuy is inferred when the retailer does not provide an explicit offer.

`Temporarily unavailable` and `Out of stock` products are reported once when both locations first agree on that state, suppressed on subsequent unchanged runs, and shown again as `Back in stock` only after both locations return to full availability.

The primary configured location is **Cheltenham VIC 3192**. Coles resolves that locality through its public location service and uses the returned fulfilment store for catalogue pricing. Woolworths receives postcode 3192 in its anonymous category request; because that response does not identify the selected store, the monitor describes those values as Woolworths online prices rather than claiming a particular store's shelf price.

Availability changes use **Broadway NSW 2007** as a verification location. Coles resolves this to its Broadway fulfilment store (`839`); Woolworths receives postcode 2007. Broadway is queried only when Cheltenham currently has an availability issue or the prior agreed state had an issue. An unavailable/restocked state is accepted only when both locations return the same state. If one location is available and the other is not, the last agreed state is carried forward. A later transition from that disagreement to matching unavailability is reported; the reverse transition from matching unavailability to a one-location recovery is not treated as a restock. A failed backup check retains the retailer's last verified snapshot.

The first successful run emails the complete baseline once. Later runs send an email only when at least one new, previously unreported change exists. Product names in the HTML email and Excel workbook link to their retailer product pages. No-change runs send nothing. Products removed entirely from a category are not treated as availability changes because neither location supplied a current product record.

## Schedule

The workflow runs at `20:00 UTC Tuesday`, which is **06:00 AEST Wednesday**. Because AEST is a fixed UTC+10 offset, this is 07:00 in Sydney when daylight saving (AEDT) applies. GitHub Actions schedules can start a few minutes late under load.

## Required GitHub repository setup

1. Create a private GitHub repository and push this folder as its root.
2. In **Settings → Secrets and variables → Actions**, add:
   - `GMAIL_APP_PASSWORD`: a Google App Password for `liamdwaas@gmail.com` (never use or commit the normal Google password).
   - `COLES_BUILD_ID`: optional fallback containing the current Coles Next.js `buildId`. The normal Coles route uses its anonymous storefront APIs, so this is used only by the category-page compatibility fallback.
3. In **Settings → Actions → General → Workflow permissions**, select **Read and write permissions** so the workflow can commit its history.
4. Pushing the initial setup creates and emails the baseline. **Actions → Weekly Coles product monitor → Run workflow** remains available for diagnostics, but a manual run does not resend an existing baseline.

Google App Passwords require 2-Step Verification. If Google Workspace policy blocks App Passwords, use an approved SMTP relay and adapt `send_email` in `coles_monitor/reporting.py`.

## Data integrity behavior

- After a baseline exists, a temporarily blocked retailer retains its last verified records while the other retailer continues normally. The workflow emits a GitHub warning, never interprets the access failure as removals, and retries the retailer on the next scheduled run.
- Coles' flag comes from `pricing.onlineSpecial`/an online promotion label; Woolworths' flag comes from `IsOnlineOnly`. The monitor does not infer this status from price differences.
- Browser-compatible anonymous sessions are used for the retailers' public storefront data routes; no login, cart or checkout access is used. Coles first resolves the configured locality and frozen-seafood taxonomy, then paginates its official public storefront product API. Its server-rendered category data remains a compatibility fallback.
- The workflow does not use or require a retail proxy.
- Both category scrapers validate pagination against the retailer's reported total before accepting a snapshot. An incomplete category traversal fails safely and retains the last verified retailer snapshot.
- Change events have deterministic IDs and are stored in `data/events.json`, preventing duplicate reports.
- The complete audit history and current combined catalogue are kept in `data/coles-woolworths-frozen-seafood-change-history.xlsx` and uploaded as a workflow artifact.
- Every search includes postcode `3192` and delivery context. Prices should be treated as online prices returned for that location, not as a claim about shelf prices at an unspecified physical store.

## Local test

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python run_monitor.py --fixture tests/fixtures/week1.json --no-email
python run_monitor.py --fixture tests/fixtures/week2.json --no-email
```

The fixture names and URLs use the reserved `example.test` domain and are tests only; they are not Coles product claims.

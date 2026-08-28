# Coles and Woolworths frozen seafood monitor

This monitor enumerates SKUs from these retailer category pages, rather than selecting products with search keywords:

- Woolworths: `https://www.woolworths.com.au/shop/browse/freezer/frozen-seafood`
- Coles: `https://www.coles.com.au/browse/frozen/frozen-fish-seafood?sortBy=recommendedDescending`

It records new SKUs and changes to product name, price, promotions, pack size, image, availability and online-only status. Reports are grouped for readability, but grouping never determines inclusion: if a SKU is returned by the configured frozen-seafood category, it is monitored.

Each successful run stores a combined snapshot, a de-duplicated event history and an Excel workbook. A retailer access failure cannot erase the last verified snapshot. The first scheduled run sends the complete baseline; later emails contain only reportable changes. A promotion merely returning to full price remains in the audit trail but is suppressed from the email body.

## Schedule and recipient

GitHub Actions runs at `20:00 UTC Tuesday`, exactly **06:00 AEST Wednesday** (fixed UTC+10). This is 07:00 in Sydney while AEDT applies. GitHub may start scheduled jobs a few minutes late.

Emails are addressed to `liam.dewaas@simplot.com` from the configured Gmail account.

## Required repository secrets

In **Settings → Secrets and variables → Actions**, add:

- `GMAIL_APP_PASSWORD`: Google App Password for `liamdwaas@gmail.com`.
- `RETAIL_PROXY_URL`: Australian residential HTTPS proxy URL. Coles commonly rejects GitHub-hosted datacenter IPs.
- `COLES_BUILD_ID`: optional fallback only; live category HTML is used directly by the current scraper.

Set **Settings → Actions → General → Workflow permissions** to **Read and write permissions** so the workflow can commit its state files. Never commit passwords or proxy credentials.

## Data integrity

- SKU inclusion comes from the two configured category pages.
- Pricing uses online delivery context for Cheltenham VIC 3192.
- Empty, malformed or blocked category responses fail safely.
- The same Australian proxy is used for both retailers when configured.
- Deterministic event IDs prevent duplicate change emails.
- Product links point to the retailer product pages.

## Local verification

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python run_monitor.py --fixture tests/fixtures/week1.json --no-email
```

Live scraping may require the same Australian proxy used by the GitHub workflow.

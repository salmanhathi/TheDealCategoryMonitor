# Product Health Monitor

Scrape-based storefront checker for e-commerce teams.

## What it detects
- Missing price
- Missing / broken images
- Missing product title
- Missing description
- HTTP errors (404, 5xx, timeouts)

## Setup

```bash
pip install -r requirements.txt
python app.py
```

Then open: http://localhost:5050

## Usage
1. Paste product URLs (one per line) into the text area
2. Click **Start Scan**
3. Filter results by issue type
4. Export to CSV for sharing

## Limits
- Max 200 URLs per scan (adjustable in app.py)
- Polite crawl: 0.3s delay between requests
- Works on any standard storefront (SFCC, Shopify, WooCommerce, Magento, etc.)

## Notes
- For SFCC: URLs should be live storefront PDP URLs (not Business Manager)
- Some sites block scrapers; add your own `Cookie` or `Authorization` header in the HEADERS dict if needed
- To schedule automatic scans, wrap `analyse_url()` calls in a cron job and email the CSV

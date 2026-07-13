from flask import Flask, render_template, request, jsonify, Response
import requests
from bs4 import BeautifulSoup
import csv, io, re, time, json
from urllib.parse import urlparse, urljoin, urlunparse, parse_qs, urlencode, urldefrag
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

SCAN_WORKERS   = 10   # parallel product scans
CRAWL_DELAY    = 0.2  # seconds between category page fetches

# ─────────────────────────────────────────────────────────────────────────────
# CATEGORY CRAWLER
# ─────────────────────────────────────────────────────────────────────────────

def is_product_url(url, base_domain):
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc != base_domain:
        return False
    path = parsed.path.lower()
    product_hints = ['/p/', '/product/', '/products/', '/item/', '/pd/']
    if any(h in path for h in product_hints):
        return True
    category_hints = ['/c/', '/category/', '/search', '/s/', 'cgid=', '/new-in', '/women', '/men', '/kids']
    if path.endswith('.html') and not any(h in url for h in category_hints):
        return True
    return False


def extract_product_links(soup, base_url, base_domain):
    links = set()
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        if not href or href.startswith('#') or href.startswith('javascript'):
            continue
        full = urljoin(base_url, href)
        full, _ = urldefrag(full)
        if is_product_url(full, base_domain):
            links.add(full)
    return links


def crawl_category(category_url, page_size=48, max_pages=20):
    parsed      = urlparse(category_url)
    base_domain = parsed.netloc
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs.pop('start', None)
    if 'sz' in qs:
        try:
            page_size = int(qs['sz'][0])
        except ValueError:
            pass
    qs['sz'] = [str(page_size)]

    all_products = set()
    logs = []
    start = 0

    for page_num in range(max_pages):
        qs['start'] = [str(start)]
        paged_qs  = urlencode({k: v[0] for k, v in qs.items()})
        page_url  = urlunparse(parsed._replace(query=paged_qs))
        logs.append(f"Fetching page {page_num + 1}: {page_url}")
        try:
            r = requests.get(page_url, headers=HEADERS, timeout=15, verify=False)
            if r.status_code != 200:
                logs.append(f"  → HTTP {r.status_code}, stopping.")
                break
            soup  = BeautifulSoup(r.text, 'html.parser')
            found = extract_product_links(soup, page_url, base_domain)
            new   = found - all_products
            logs.append(f"  → Found {len(found)} links ({len(new)} new)")
            if not new:
                logs.append("  → No new products — pagination complete.")
                break
            all_products.update(new)
            start += page_size
            time.sleep(CRAWL_DELAY)
        except Exception as e:
            logs.append(f"  → Error: {e}")
            break

    return sorted(all_products), logs


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACTORS  —  tuned for thedealoutlet.com SFCC structure
# ─────────────────────────────────────────────────────────────────────────────

def extract_prices(soup):
    """
    Returns dict: { sale_price, original_price, saving, has_sale }
    Targets TDO structure:
      Sale:     h2.sales span.value[content]
      Original: span.strike-through.list span.value[content]
      Saving:   .wis_fiyatfark
    Falls back to generic selectors for other sites.
    """
    result = {
        "sale_price":     None,
        "original_price": None,
        "saving":         None,
        "has_sale":       False,
    }

    # ── TDO / SFCC specific ──────────────────────────────────────────────────
    sale_el = soup.select_one('h2.sales span.value, .sales span.value')
    if sale_el:
        result["sale_price"] = sale_el.get('content') or re.sub(r'[^\d.]', '', sale_el.get_text())

    orig_el = soup.select_one('span.strike-through.list span.value, .strike-through .value')
    if orig_el:
        result["original_price"] = orig_el.get('content') or re.sub(r'[^\d.]', '', orig_el.get_text())

    saving_el = soup.select_one('.wis_fiyatfark')
    if saving_el:
        result["saving"] = saving_el.get_text(strip=True)

    if result["sale_price"] and result["original_price"]:
        result["has_sale"] = True
        return result

    # ── Generic fallbacks ────────────────────────────────────────────────────
    if not result["sale_price"]:
        for sel in ['[itemprop="price"]', '.price-sales', '.special-price .price',
                    'meta[property="product:price:amount"]']:
            el = soup.select_one(sel)
            if el:
                v = el.get('content') or re.sub(r'[^\d.]', '', el.get_text())
                if v:
                    result["sale_price"] = v
                    break

    if not result["original_price"]:
        for sel in ['.price-was', '.regular-price .price', '.old-price .price',
                    '.price__compare']:
            el = soup.select_one(sel)
            if el:
                v = re.sub(r'[^\d.]', '', el.get_text())
                if v:
                    result["original_price"] = v
                    break

    # JSON-LD last resort
    if not result["sale_price"]:
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.string or '')
                if isinstance(data, dict):
                    offers = data.get('offers', {})
                    if isinstance(offers, dict):
                        p = offers.get('price') or offers.get('lowPrice')
                        if p:
                            result["sale_price"] = str(p)
                            break
            except Exception:
                pass

    result["has_sale"] = bool(result["sale_price"] and result["original_price"])
    return result


def extract_images(soup, base_url):
    """
    Priority order:
    1. TDO/SFCC product image containers (desktop + mobile)
    2. og:image meta
    3. schema.org itemprop=image
    4. Common CMS class names
    Filters out SVGs, data URIs, and TDO's 'noimagelarge.png' placeholder.
    """
    images = []
    seen   = set()

    # TDO SFCC serves this when no product image exists — treat as missing
    PLACEHOLDER_SIGNALS = [
        'noimagelarge',
        'noimage',
        'no_image',
        '/default/dw58870029/',   # TDO's specific no-image asset hash
        'blank.gif', '1x1', 'pixel',
    ]

    def is_placeholder(tag, src):
        alt   = (tag.get('alt')   or '').strip().lower()
        title = (tag.get('title') or '').strip().lower()
        if alt in ('no image', 'noimage') or title in ('no image', 'noimage'):
            return True
        low = src.lower()
        return any(p in low for p in PLACEHOLDER_SIGNALS)

    def add(tag, src=None):
        if tag is None:
            # called with just a URL string (og:image)
            if not src:
                return
            full = urljoin(base_url, src.strip())
            if full in seen:
                return
            low = full.lower()
            if any(x in low for x in ['data:', '.svg'] + PLACEHOLDER_SIGNALS):
                return
            seen.add(full)
            images.append(full)
            return

        raw = (tag.get('src') or tag.get('data-src') or tag.get('data-zoom-image')
               or tag.get('data-lazy') or tag.get('data-original') or tag.get('content') or '')
        if not raw:
            return
        full = urljoin(base_url, raw.strip())
        if full in seen:
            return
        low = full.lower()
        if any(x in low for x in ['data:', '.svg']):
            return
        if is_placeholder(tag, full):
            return
        seen.add(full)
        images.append(full)

    # TDO desktop: most reliable — real images only, no carousel clones
    for tag in soup.select('.product-images-desktop img, .js-img-parent-div img'):
        add(tag)

    # TDO/SFCC generic containers
    for sel in [
        '.primary-images img',
        '.pdp-images img',
        '.image-container img',
        '[data-image-role="product"]',
        '.product-gallery img',
    ]:
        for tag in soup.select(sel):
            add(tag)

    # og:image — skip if it's also a placeholder URL
    og = soup.find('meta', property='og:image')
    if og and og.get('content'):
        src = og['content']
        if not any(p in src.lower() for p in PLACEHOLDER_SIGNALS):
            add(None, src)

    # schema.org itemprop=image (TDO uses this too, but slick clones duplicates — deduped by seen set)
    for el in soup.select('[itemprop="image"]'):
        add(el)

    # generic fallbacks
    for sel in ['.carousel img', '.slick-slide img', '.swiper-slide img']:
        for tag in soup.select(sel):
            add(tag)

    return images


def check_image_accessible(url):
    try:
        r = requests.head(url, headers=HEADERS, timeout=5, allow_redirects=True, verify=False)
        return r.status_code == 200
    except Exception:
        return False


def extract_title(soup):
    og = soup.find('meta', property='og:title')
    if og and og.get('content'):
        return og['content'].strip()
    h1 = soup.find('h1')
    if h1:
        return h1.get_text(strip=True)
    return soup.title.string.strip() if soup.title else None


def extract_description(soup):
    meta = soup.find('meta', attrs={'name': 'description'})
    if meta and meta.get('content'):
        return meta['content'].strip()
    og = soup.find('meta', property='og:description')
    if og and og.get('content'):
        return og['content'].strip()
    for sel in ['[itemprop="description"]', '.product-description',
                '.pdp-description', '.product__description', '.short-description']:
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(strip=True)
            if txt:
                return txt[:300]
    return None


# ─────────────────────────────────────────────────────────────────────────────
# ANALYSER
# ─────────────────────────────────────────────────────────────────────────────

def analyse_url(url):
    result = {
        "url": url, "title": None,
        "sale_price": None, "original_price": None, "saving": None, "has_sale": False,
        "images": [], "image_count": 0, "broken_images": [],
        "description": None, "issues": [], "status": "ok",
        "http_status": None,
        "scraped_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
        result["http_status"] = r.status_code
        if r.status_code != 200:
            result["issues"].append(f"HTTP {r.status_code}")
            result["status"] = "error"
            return result

        soup = BeautifulSoup(r.text, 'html.parser')

        result["title"]       = extract_title(soup)
        result["description"] = extract_description(soup)
        result["images"]      = extract_images(soup, url)
        result["image_count"] = len(result["images"])

        prices = extract_prices(soup)
        result.update(prices)

        # ── Issue checks ──────────────────────────────────────────────────────
        if not result["title"]:
            result["issues"].append("Missing title")

        if not result["sale_price"] and not result["original_price"]:
            result["issues"].append("Missing price")
        elif not result["sale_price"]:
            result["issues"].append("Missing sale price")
        elif not result["original_price"]:
            result["issues"].append("Missing original price")

        if result["image_count"] == 0:
            result["issues"].append("No images found")
        else:
            broken = [img for img in result["images"] if not check_image_accessible(img)]
            result["broken_images"] = broken
            if broken:
                result["issues"].append(f"{len(broken)} broken image(s)")

        if not result["description"]:
            result["issues"].append("Missing description")

        result["status"] = "issues" if result["issues"] else "ok"

    except requests.exceptions.Timeout:
        result["issues"].append("Timeout (>15s)"); result["status"] = "error"
    except requests.exceptions.ConnectionError:
        result["issues"].append("Connection error"); result["status"] = "error"
    except Exception as e:
        result["issues"].append(f"Error: {str(e)[:80]}"); result["status"] = "error"

    return result


def build_summary(results):
    return {
        "total":                len(results),
        "ok":                   sum(1 for r in results if r["status"] == "ok"),
        "issues":               sum(1 for r in results if r["status"] == "issues"),
        "errors":               sum(1 for r in results if r["status"] == "error"),
        "missing_price":        sum(1 for r in results if "Missing price" in r["issues"]),
        "missing_sale_price":   sum(1 for r in results if "Missing sale price" in r["issues"]),
        "missing_orig_price":   sum(1 for r in results if "Missing original price" in r["issues"]),
        "missing_images":       sum(1 for r in results if "No images found" in r["issues"]),
        "broken_images":        sum(1 for r in results if any("broken image" in i for i in r["issues"])),
        "missing_desc":         sum(1 for r in results if "Missing description" in r["issues"]),
    }


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/discover', methods=['POST'])
def discover():
    data         = request.get_json()
    category_url = (data.get('category_url') or '').strip()
    if not category_url:
        return jsonify({"error": "No category URL provided"}), 400
    if not category_url.startswith('http'):
        category_url = 'https://' + category_url
    page_size = int(data.get('page_size', 48))
    max_pages = int(data.get('max_pages', 20))
    product_urls, logs = crawl_category(category_url, page_size, max_pages)
    return jsonify({"product_urls": product_urls, "logs": logs, "count": len(product_urls)})


@app.route('/scan', methods=['POST'])
def scan():
    data = request.get_json()
    urls = [u.strip() for u in (data.get('urls') or '').splitlines() if u.strip()]
    if not urls:
        return jsonify({"error": "No URLs provided"}), 400
    if len(urls) > 5000:
        return jsonify({"error": "Max 5000 URLs per scan"}), 400

    results = [None] * len(urls)

    def worker(idx, url):
        if not url.startswith('http'):
            url = 'https://' + url
        return idx, analyse_url(url)

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        futures = {ex.submit(worker, i, u): i for i, u in enumerate(urls)}
        for future in as_completed(futures):
            idx, res = future.result()
            results[idx] = res

    return jsonify({"results": results, "summary": build_summary(results)})


@app.route('/export-csv', methods=['POST'])
def export_csv():
    data    = request.get_json()
    results = data.get('results', [])
    output  = io.StringIO()
    writer  = csv.DictWriter(output, fieldnames=[
        'url', 'title', 'sale_price', 'original_price', 'saving',
        'image_count', 'broken_images', 'description_present',
        'issues', 'status', 'http_status', 'scraped_at'
    ])
    writer.writeheader()
    for r in results:
        writer.writerow({
            'url':                  r.get('url', ''),
            'title':                r.get('title', ''),
            'sale_price':           r.get('sale_price', ''),
            'original_price':       r.get('original_price', ''),
            'saving':               r.get('saving', ''),
            'image_count':          r.get('image_count', 0),
            'broken_images':        '; '.join(r.get('broken_images', [])),
            'description_present':  'Yes' if r.get('description') else 'No',
            'issues':               '; '.join(r.get('issues', [])),
            'status':               r.get('status', ''),
            'http_status':          r.get('http_status', ''),
            'scraped_at':           r.get('scraped_at', ''),
        })
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Response(output.getvalue(), mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=product_health_{ts}.csv'})


if __name__ == '__main__':
    app.run(debug=True, port=5050)

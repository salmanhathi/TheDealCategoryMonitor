from flask import Flask, render_template, request, jsonify, Response
import requests
from bs4 import BeautifulSoup
import csv, io, re, time, json, threading, uuid
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

SCAN_WORKERS = 15
CRAWL_DELAY  = 0.2
MAX_PRODUCTS = 5000

# ─────────────────────────────────────────────────────────────────────────────
# CATEGORY CRAWLER
# ─────────────────────────────────────────────────────────────────────────────

BLOCKED_SLUGS = [
    'about-us', 'faq', 'privacy', 'privacy-policy', 'terms-conditions',
    'return-refunds', 'order-shipping', 'contact', 'careers', 'stores',
    'accessibility', 'sitemap', 'gift-card',
]

def is_product_url(url, base_domain):
    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc != base_domain:
        return False
    path = parsed.path.lower()

    if any(s in path for s in BLOCKED_SLUGS):
        return False

    # TDO: numeric barcode filename of any length (6+ digits)
    if re.search(r'/\d{6,}\.html$', path):
        return True

    product_hints = ['/p/', '/product/', '/products/', '/item/', '/pd/']
    if any(h in path for h in product_hints):
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


def crawl_category(category_url, page_size=48, max_pages=50):
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
        paged_qs = urlencode({k: v[0] for k, v in qs.items()})
        page_url = urlunparse(parsed._replace(query=paged_qs))
        logs.append(f"  Page {page_num + 1}: {page_url}")
        try:
            r = requests.get(page_url, headers=HEADERS, timeout=15, verify=False)
            if r.status_code != 200:
                logs.append(f"    → HTTP {r.status_code}, stopping.")
                break
            soup  = BeautifulSoup(r.text, 'html.parser')
            found = extract_product_links(soup, page_url, base_domain)
            new   = found - all_products
            logs.append(f"    → {len(found)} links found, {len(new)} new")
            if not new:
                logs.append("    → No new products — pagination complete.")
                break
            all_products.update(new)
            start += page_size
            time.sleep(CRAWL_DELAY)
        except Exception as e:
            logs.append(f"    → Error: {e}")
            break

    return sorted(all_products), logs


def crawl_multiple_categories(cat_urls, page_size=48, max_pages=50):
    all_products = set()
    all_logs = []
    for cat_url in cat_urls:
        if not cat_url.startswith('http'):
            cat_url = 'https://' + cat_url
        all_logs.append(f"═══ Crawling: {cat_url}")
        urls, logs = crawl_category(cat_url, page_size, max_pages)
        new = set(urls) - all_products
        all_products.update(new)
        all_logs.extend(logs)
        all_logs.append(f"  +{len(new)} new products | {len(all_products)} total so far")
        if len(all_products) >= MAX_PRODUCTS:
            all_logs.append(f"  Cap of {MAX_PRODUCTS} reached, stopping.")
            break
    return sorted(all_products), all_logs


# ─────────────────────────────────────────────────────────────────────────────
# EXTRACTORS — tuned for thedealoutlet.com SFCC
# ─────────────────────────────────────────────────────────────────────────────

def extract_prices(soup):
    result = {
        "sale_price": None, "original_price": None,
        "saving": None, "has_sale": False,
    }

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
        for sel in ['.price-was', '.regular-price .price', '.old-price .price', '.price__compare']:
            el = soup.select_one(sel)
            if el:
                v = re.sub(r'[^\d.]', '', el.get_text())
                if v:
                    result["original_price"] = v
                    break

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
    images = []
    seen   = set()

    PLACEHOLDER_SIGNALS = [
        'noimagelarge', 'noimage', 'no_image',
        '/default/dw58870029/',
        'blank.gif', '1x1', 'pixel',
    ]

    def is_placeholder(tag, src):
        alt   = (tag.get('alt')   or '').strip().lower()
        title = (tag.get('title') or '').strip().lower()
        if alt in ('no image', 'noimage') or title in ('no image', 'noimage'):
            return True
        return any(p in src.lower() for p in PLACEHOLDER_SIGNALS)

    def add(tag, src=None):
        if tag is None:
            if not src:
                return
            full = urljoin(base_url, src.strip())
            if full in seen:
                return
            if any(x in full.lower() for x in ['data:', '.svg'] + PLACEHOLDER_SIGNALS):
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
        if any(x in full.lower() for x in ['data:', '.svg']):
            return
        if is_placeholder(tag, full):
            return
        seen.add(full)
        images.append(full)

    for tag in soup.select('.product-images-desktop img, .js-img-parent-div img'):
        add(tag)

    for sel in ['.primary-images img', '.pdp-images img', '.image-container img',
                '[data-image-role="product"]', '.product-gallery img']:
        for tag in soup.select(sel):
            add(tag)

    og = soup.find('meta', property='og:image')
    if og and og.get('content'):
        src = og['content']
        if not any(p in src.lower() for p in PLACEHOLDER_SIGNALS):
            add(None, src)

    for el in soup.select('[itemprop="image"]'):
        add(el)

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
    data      = request.get_json()
    raw_urls  = (data.get('category_urls') or data.get('category_url') or '')
    page_size = int(data.get('page_size', 48))
    cat_urls  = [u.strip() for u in raw_urls.splitlines() if u.strip()]
    if not cat_urls:
        return jsonify({"error": "No category URLs provided"}), 400
    product_urls, logs = crawl_multiple_categories(cat_urls, page_size)
    return jsonify({"product_urls": product_urls, "logs": logs, "count": len(product_urls)})


# Job store for streaming scan
_scan_jobs = {}

@app.route('/scan-prepare', methods=['POST'])
def scan_prepare():
    data = request.get_json()
    urls = [u.strip() for u in (data.get('urls') or '').splitlines() if u.strip()]
    if not urls:
        return jsonify({"error": "No URLs provided"}), 400
    if len(urls) > MAX_PRODUCTS:
        return jsonify({"error": f"Max {MAX_PRODUCTS} URLs per scan"}), 400
    job_id = str(uuid.uuid4())
    _scan_jobs[job_id] = urls
    return jsonify({"job_id": job_id, "count": len(urls)})


@app.route('/scan-stream', methods=['GET'])
def scan_stream():
    job_id = request.args.get('job_id')
    if not job_id or job_id not in _scan_jobs:
        return Response('data: {"error": "Invalid job"}\n\n', mimetype='text/event-stream')
    urls = _scan_jobs.pop(job_id)

    def generate():
        completed = []
        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
            futures = {ex.submit(analyse_url, u): u for u in urls}
            for future in as_completed(futures):
                result = future.result()
                completed.append(result)
                yield f"data: {json.dumps({'type': 'result', 'data': result})}\n\n"
        summary = build_summary(completed)
        yield f"data: {json.dumps({'type': 'done', 'summary': summary})}\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache'})


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

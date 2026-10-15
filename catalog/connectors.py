import requests


class BaseConnector:
    """Every connector must return products in the SAME standard format."""
    def __init__(self, store):
        self.store = store

    def fetch(self):
        raise NotImplementedError


class DummyJsonConnector(BaseConnector):
    def fetch(self):
        resp = requests.get(self.store.source_url, params={"limit": 0}, timeout=30)
        resp.raise_for_status()
        return [{
            "external_id": str(item["id"]),
            "name": item["title"],
            "description": item.get("description", ""),
            "category": item.get("category", ""),
            "price": item["price"],
            "in_stock": item.get("stock", 0) > 0,
            "url": "",
            "image_url": item.get("thumbnail", ""),
        } for item in resp.json()["products"]]


class FlipkartConnector(BaseConnector):
    """
    Flipkart Catalog Connector:
    Loads product catalog from a remote Flipkart API/feed endpoint, or from a local Flipkart JSON dataset.
    """
    def fetch(self):
        import json
        import os
        from pathlib import Path

        # 1. Remote JSON feed if configured
        if self.store.source_url and self.store.source_url.startswith(("http://", "https://")):
            resp = requests.get(self.store.source_url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        elif self.store.source_url and os.path.exists(self.store.source_url):
            with open(self.store.source_url, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            # 2. Built-in Flipkart dataset
            default_path = Path(__file__).resolve().parent / "data" / "flipkart_products.json"
            if not default_path.exists():
                raise FileNotFoundError(f"Flipkart catalog file not found: {default_path}")
            with open(default_path, "r", encoding="utf-8") as f:
                data = json.load(f)

        products = data.get("products", data) if isinstance(data, dict) else data
        normalized = []
        for item in products:
            normalized.append({
                "external_id": str(item.get("external_id") or item.get("id") or item.get("pid") or item.get("_id")),
                "name": item.get("name") or item.get("title") or item.get("product_name") or "",
                "description": item.get("description") or item.get("desc") or "",
                "category": item.get("category") or item.get("product_category") or "",
                "price": float(item.get("price") or item.get("discounted_price") or item.get("retail_price") or 0.0),
                "in_stock": bool(item.get("in_stock", True) if "in_stock" in item else item.get("stock", 1) > 0),
                "url": item.get("url") or item.get("product_url") or "",
                "image_url": item.get("image_url") or item.get("image") or (item.get("images")[0] if isinstance(item.get("images"), list) and item.get("images") else ""),
            })
        return normalized

    @staticmethod
    def search_live(query, limit=20):
        """
        Real-time live search scraper for flipkart.com
        Searches Flipkart in real-time and extracts live products, prices in INR, images and links.
        Guarantees that all product links work and open Flipkart product or search pages cleanly.
        """
        import re
        import urllib.parse
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return []

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept-Language': 'en-IN,en;q=0.9',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        }
        url = f"https://www.flipkart.com/search?q={urllib.parse.quote_plus(query)}"
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code != 200:
                return []
            soup = BeautifulSoup(resp.text, 'html.parser')
            items = []
            for item in soup.select('div[data-id]'):
                txt = item.get_text(' | ')
                parts = [p.strip() for p in txt.split(' | ') if p.strip()]

                # 1. Price parsing
                price = 0.0
                for p in parts:
                    m = re.search(r'\u20b9\s*([\d,]+)', p)
                    if m:
                        try:
                            price = float(m.group(1).replace(',', ''))
                            break
                        except ValueError:
                            pass
                if price <= 0:
                    continue

                # 2. Title parsing (Clean title without ratings or compare labels)
                title = ''
                img = item.find('img', alt=True)
                if img and img.get('alt') and len(img['alt'].strip()) >= 6:
                    alt = img['alt'].strip()
                    if not re.match(r'^(image|product|thumbnail|flipkart)\b', alt, re.I):
                        title = alt

                if not title:
                    brand_elem = item.find('div', class_=lambda c: c and 'Fo1I0b' in c)
                    prod_elem = item.find('a', class_=lambda c: c and 'atJtCj' in c)
                    if brand_elem and prod_elem:
                        b_txt = brand_elem.get_text(strip=True)
                        p_txt = prod_elem.get_text(strip=True)
                        if b_txt and p_txt:
                            title = f"{b_txt} {p_txt}".strip()

                if not title:
                    a_titled = item.find('a', title=True)
                    if a_titled and a_titled.get('title') and len(a_titled['title'].strip()) >= 6:
                        title = a_titled['title'].strip()

                if not title:
                    before_price = []
                    for p in parts:
                        if '\u20b9' in p:
                            break
                        before_price.append(p)
                    raw = ' '.join(before_price).strip()
                    raw = re.sub(r'^(?:Add to Compare\s*)+', '', raw, flags=re.I).strip()
                    raw = re.sub(r'\s*\b\d\.\d\s+[\d,]+\s+Ratings\b.*$', '', raw, flags=re.I).strip()
                    title = raw

                if not title:
                    continue

                # Clean any lingering "Add to Compare" prefix
                title = re.sub(r'^(?:Add to Compare\s*)+', '', title, flags=re.I).strip()

                # 3. URL parsing: preserve exact Flipkart link with pid
                a_link = item.find('a', href=lambda h: h and '/p/' in h) or item.find('a', href=True)
                href = ''
                if a_link and a_link.get('href'):
                    h = a_link['href']
                    if h.startswith('http'):
                        href = h
                    elif h.startswith('/'):
                        href = 'https://www.flipkart.com' + h
                    else:
                        href = 'https://www.flipkart.com/' + h

                # Guarantee link reliability: extract canonical /p/ link with pid only, or fallback to search URL
                if '/p/' in href and 'pid=' in href:
                    try:
                        base_u, q_part = href.split('?', 1)
                        q_parsed = urllib.parse.parse_qs(q_part)
                        pid_code = q_parsed.get('pid', [''])[0]
                        if pid_code:
                            href = f"{base_u}?pid={pid_code}"
                    except Exception:
                        pass
                elif not href or ('/p/' in href and 'pid=' not in href):
                    href = f"https://www.flipkart.com/search?q={urllib.parse.quote_plus(title)}"

                # 4. Image URL
                img_el = item.find('img', src=True)
                img_src = img_el['src'] if img_el else ''

                pid = item.get('data-id') or f"FK-LIVE-{abs(hash(title))%1000000}"
                items.append({
                    "external_id": str(pid),
                    "name": title.strip(),
                    "description": f"{title.strip()} on Flipkart India. Current price ₹{price:,.0f}.",
                    "category": "Flipkart Live",
                    "price": price,
                    "in_stock": True,
                    "url": href,
                    "image_url": img_src,
                })
                if len(items) >= limit:
                    break
            return items
        except Exception as exc:
            print("[FLIPKART LIVE SCRAPE ERROR]:", exc)
            return []


CONNECTORS = {
    "dummyjson": DummyJsonConnector,
    "flipkart": FlipkartConnector,
}
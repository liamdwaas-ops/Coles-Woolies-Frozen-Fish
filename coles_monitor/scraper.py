import html
import json
import os
import re
import time
from urllib.parse import parse_qs, urlencode, urlparse

from curl_cffi import requests

from .matcher import is_allowed_product, normalize, split_name_size
from .promotions import find_multibuy_text, multibuy_unit_price


BASE_URL = "https://www.coles.com.au"
USER_AGENT = "Mozilla/5.0 (compatible; FrozenSeafoodMonitor/1.0; personal-use)"


class ScrapeError(RuntimeError):
    pass


class ColesScraper:
    def __init__(self, delay=1.0, max_pages=20, page_size=48, location=None,
                 verified_build_id_fallback=""):
        self.delay = delay
        self.max_pages = max_pages
        self.page_size = page_size
        self.location = location or {}
        self.proxy_url = os.getenv("RETAIL_PROXY_URL", "").strip()
        self.session = self._new_session()
        self.session.headers.update({"Accept": "application/json,text/html"})
        self.build_id = (os.getenv("COLES_BUILD_ID", "").strip() or
                         normalize(verified_build_id_fallback))

    def _new_session(self):
        kwargs = {"impersonate": "chrome"}
        if self.proxy_url:
            kwargs["proxy"] = self.proxy_url
        return requests.Session(**kwargs)

    def _get(self, url, **kwargs):
        response = self.session.get(url, timeout=35, **kwargs)
        response.raise_for_status()
        return response

    def discover_build_id(self, force=False):
        if self.build_id and not force:
            return self.build_id
        if force:
            self.build_id = ""
        for _attempt in range(5):
            self.session = self._new_session()
            self.session.headers.update({"Accept": "application/json,text/html"})
            for path in ("/", "/browse/frozen/frozen-fish-seafood"):
                try:
                    text = self._get(
                        BASE_URL + path,
                        headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
                    ).text
                except requests.RequestsError:
                    continue
                match = re.search(r'"buildId"\s*:\s*"([^"]+)"', text)
                if match:
                    self.build_id = html.unescape(match.group(1))
                    return self.build_id
            time.sleep(2)
        raise ScrapeError(
            "Coles blocked build-ID discovery. Set the repository secret COLES_BUILD_ID "
            "to the current buildId from the Coles page source. No report was generated."
        )

    @staticmethod
    def _find_results(payload):
        page_props = payload.get("pageProps", {})
        likely = [page_props.get("searchResults"), page_props.get("results"), page_props.get("products")]
        for value in likely:
            if isinstance(value, dict):
                for key in ("results", "products", "items"):
                    if isinstance(value.get(key), list):
                        return value[key], value
            if isinstance(value, list):
                return value, {}
        stack = [page_props]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in ("results", "products", "items") and isinstance(child, list):
                        if any(isinstance(x, dict) and ("name" in x or "id" in x) for x in child):
                            return child, value
                    stack.append(child)
            elif isinstance(value, list):
                stack.extend(value)
        raise ScrapeError("Coles returned JSON, but its product structure was not recognised.")

    @staticmethod
    def _product(raw):
        pricing = raw.get("pricing") or raw.get("price") or {}
        if not isinstance(pricing, dict):
            pricing = {"now": pricing}
        raw_name = raw.get("name") or raw.get("title") or raw.get("description") or ""
        name, size = split_name_size(raw_name, raw.get("size") or raw.get("packageSize") or "")
        product_id = str(raw.get("id") or raw.get("productId") or raw.get("code") or "").strip()
        slug = normalize(raw.get("slug") or raw.get("seoToken") or "")
        uri = ""
        images = raw.get("imageUris") or raw.get("images") or []
        if images:
            first = images[0]
            uri = first.get("uri", "") if isinstance(first, dict) else str(first)
        uri = raw.get("imageUrl") or raw.get("image_url") or uri
        if uri.startswith("/"):
            uri = "https://cdn.productimages.coles.com.au/productimages" + uri
        product_url = raw.get("url") or (f"{BASE_URL}/product/{slug}" if slug else "")
        if product_url.startswith("/"):
            product_url = BASE_URL + product_url
        if not product_url and product_id:
            product_url = f"{BASE_URL}/product/{product_id}"
        price = pricing.get("now", pricing.get("value", pricing.get("current")))
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = normalize(price)
        was = pricing.get("was")
        try:
            was = float(was) if was is not None else None
        except (TypeError, ValueError):
            was = None
        promotion_type = normalize(pricing.get("promotionType")).upper()
        multibuy_text = find_multibuy_text(pricing)
        multibuy_price = multibuy_unit_price(multibuy_text)
        is_multibuy = bool(multibuy_text and isinstance(price, (int, float)))
        is_promo = bool(was and isinstance(price, (int, float)) and was > price and
                        (promotion_type in {"SPECIAL", "PERCENT_OFF"} or
                         pricing.get("onlineSpecial")))
        availability_type = normalize(raw.get("availabilityType"))
        available = bool(raw.get("availability"))
        status_text = availability_type.lower().replace("_", "").replace(" ", "")
        if available:
            availability_state = "in_stock"
        elif "temporar" in status_text:
            availability_state = "temporary_unavailable"
        else:
            availability_state = "out_of_stock"
        return product_id, {
            "retailer": "Coles", "brand": normalize(raw.get("brand")), "name": name,
            "price": price,
            "original_price": price if is_multibuy else (was if is_promo else None),
            "promotional_price": multibuy_text if is_multibuy else (price if is_promo else None),
            "discount_percent": (round((price - multibuy_price) / price, 4)
                                   if is_multibuy and multibuy_price is not None and price > multibuy_price
                                   else (round((was - price) / was, 4) if is_promo else None)),
            "availability_state": availability_state,
            "availability_label": availability_type or ("Available" if available else "Out of stock"),
            "size": size, "image_url": uri,
            "online_only": bool(pricing.get("onlineSpecial")) or
                           "ONLINE" in normalize(pricing.get("promotionType")).upper(),
            "product_url": product_url, "source": product_url,
        }

    def scrape(self, category_url):
        """Enumerate SKUs from the supplied Coles browse page, page by page."""
        found = {}
        parsed = urlparse(category_url)
        category_path = parsed.path
        base_query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        for page in range(1, self.max_pages + 1):
            params = dict(base_query)
            if self.location.get("postcode"):
                params["postcode"] = self.location["postcode"]
            if self.location.get("state"):
                params["state"] = self.location["state"]
            if self.location.get("context_mode"):
                params["contextMode"] = self.location["context_mode"]
            if page > 1:
                params["page"] = page
            page_url = BASE_URL + category_path
            if params:
                page_url += "?" + urlencode(params)
            match = None
            for attempt in range(5):
                response = self._get(page_url, headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                })
                match = re.search(
                    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                    response.text, re.S,
                )
                if match:
                    break
                self.session = self._new_session()
                time.sleep(2 * (attempt + 1))
            if not match:
                raise ScrapeError(
                    f"Coles category page {page} did not contain its product data."
                )
            try:
                payload = json.loads(html.unescape(match.group(1)))
            except (ValueError, TypeError) as exc:
                raise ScrapeError("Coles category product data was malformed.") from exc
            page_props = payload.get("props", {}).get("pageProps", payload)
            results, metadata = self._find_results({"pageProps": page_props})
            if not results:
                break
            for raw in results:
                if not isinstance(raw, dict):
                    continue
                product_id, product = self._product(raw)
                if product_id and is_allowed_product(product["name"], product["brand"]):
                    found["coles:" + product_id] = product
            total = (metadata.get("totalResults") or metadata.get("noOfResults") or
                     metadata.get("total") or metadata.get("totalCount"))
            if total is not None and page * self.page_size >= int(total):
                break
            if len(results) < self.page_size:
                break
            time.sleep(self.delay)
        if not found:
            raise ScrapeError("Coles returned no matching products. Snapshot was not replaced.")
        missing = [pid for pid, p in found.items() if not p["name"] or not p["product_url"]]
        if missing:
            raise ScrapeError(f"Coles returned incomplete records for {len(missing)} products. Snapshot was not replaced.")
        return found

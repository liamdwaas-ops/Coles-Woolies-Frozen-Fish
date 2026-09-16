import html
import json
import os
import re
import time
import uuid
from urllib.parse import parse_qsl, quote, urlencode, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests

from .matcher import category_group, is_allowed_product, normalize, split_name_size
from .promotions import find_multibuy_text, multibuy_unit_price


BASE_URL = "https://www.coles.com.au"
CATEGORY_URL = BASE_URL + "/browse/frozen/frozen-fish-seafood?sortBy=recommendedDescending"
USER_AGENT = "Mozilla/5.0 (compatible; ColesProductChangeMonitor/1.0; personal-use)"
BFF_BASE_URL = BASE_URL + "/api/bff"
GRAPHQL_URL = BASE_URL + "/api/graphql"
RUNTIME_CONFIG_URL = BASE_URL + "/statuscheck"


class ScrapeError(RuntimeError):
    pass


class ColesScraper:
    def __init__(self, delay=1.0, max_pages=20, page_size=48, location=None,
                 verified_build_id_fallback="", category_url=CATEGORY_URL):
        self.delay = delay
        self.max_pages = max_pages
        self.page_size = page_size
        self.location = location or {}
        self.session = self._new_session()
        self.session.headers.update({"Accept": "application/json,text/html"})
        self.api_session_id = str(uuid.uuid4())
        self.api_visitor_id = str(uuid.uuid4())
        self.bff_subscription_key = os.getenv(
            "COLES_BFF_SUBSCRIPTION_KEY", ""
        ).strip()
        self.build_id = (os.getenv("COLES_BUILD_ID", "").strip() or
                         normalize(verified_build_id_fallback))
        self.category_url = category_url or CATEGORY_URL

    def _new_session(self):
        return requests.Session(impersonate="chrome")

    def _get(self, url, **kwargs):
        response = self.session.get(url, timeout=35, **kwargs)
        response.raise_for_status()
        return response

    def _public_api_headers(self):
        """Return the headers used by Coles' own anonymous storefront client."""
        if not self.bff_subscription_key:
            response = self._get(
                RUNTIME_CONFIG_URL,
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
            match = re.search(
                r'"BFF_API_SUBSCRIPTION_KEY"\s*:\s*"([^"]+)"', response.text
            )
            if not match:
                raise ScrapeError(
                    "Coles' public storefront API configuration was unavailable."
                )
            self.bff_subscription_key = html.unescape(match.group(1))
        return {
            "Accept": "application/json",
            "Ocp-Apim-Subscription-Key": self.bff_subscription_key,
            "dsch-channel": "coles.web",
            "x-api-version": "2",
            "x-correlation-id": str(uuid.uuid4()),
            "x-session-id": self.api_session_id,
            "x-visitor-id": self.api_visitor_id,
            "Referer": self.category_url,
        }

    def _api_get(self, path, params=None):
        response = self._get(
            BFF_BASE_URL + path,
            params=params or {},
            headers=self._public_api_headers(),
        )
        if "json" not in response.headers.get("content-type", "").lower():
            raise ScrapeError(f"Coles returned non-JSON data for {path}.")
        return response.json()

    def _resolve_store_id(self):
        configured = normalize(
            self.location.get("coles_store_id") or os.getenv("COLES_STORE_ID", "")
        )
        if configured:
            return configured.removeprefix("COL:")

        postcode = normalize(self.location.get("postcode"))
        suburb = normalize(self.location.get("suburb"))
        state = normalize(self.location.get("state"))
        if not postcode:
            raise ScrapeError("A postcode is required to select a Coles pricing store.")

        suggestions = self._api_get(
            "/locations/search/suggestions",
            {"limit": 10, "searchTerm": postcode},
        ).get("localities", [])
        exact = [
            item for item in suggestions
            if normalize(item.get("postcode")) == postcode
            and (not suburb or normalize(item.get("suburb")).casefold() == suburb.casefold())
            and (not state or normalize(item.get("state")).casefold() == state.casefold())
        ]
        if not exact:
            raise ScrapeError(
                f"Coles did not return an exact locality for {suburb} {state} {postcode}."
            )
        locality = exact[0]
        locations = self._api_get(
            "/locations/search",
            {"latitude": locality["latitude"], "longitude": locality["longitude"]},
        ).get("locations", [])
        matches = [
            item for item in locations
            if normalize(item.get("postcode")) == postcode
            and item.get("fulfillmentStore", {}).get("storeId")
        ]
        if not matches:
            raise ScrapeError(
                f"Coles did not return a fulfilment store for postcode {postcode}."
            )
        matches.sort(key=lambda item: float(item.get("distance", {}).get("measurement") or 1e9))
        return normalize(matches[0]["fulfillmentStore"]["storeId"])

    def _resolve_category(self, store_id):
        query = """
            query GetProductCategories(
              $storeId: BrandedId!, $withCampaignLinks: Boolean!
            ) {
              productCategories(
                storeId: $storeId, withCampaignLinks: $withCampaignLinks
              ) {
                catalogGroupView {
                  id name level seoToken
                  catalogGroupView {
                    id name level seoToken
                    catalogGroupView { id name level seoToken }
                  }
                }
              }
            }
        """
        response = self.session.post(
            GRAPHQL_URL,
            json={
                "query": query,
                "variables": {
                    "storeId": "COL:" + store_id,
                    "withCampaignLinks": False,
                },
            },
            headers={**self._public_api_headers(), "Content-Type": "application/json"},
            timeout=35,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise ScrapeError("Coles' category taxonomy API returned an error.")
        nodes = (payload.get("data", {}).get("productCategories", {})
                 .get("catalogGroupView", []))
        tokens = [part for part in urlparse(self.category_url).path.split("/")
                  if part and part != "browse"]
        selected = None
        for token in tokens:
            selected = next(
                (item for item in nodes if normalize(item.get("seoToken")) == token),
                None,
            )
            if not selected:
                break
            nodes = selected.get("catalogGroupView") or []
        if not selected or not tokens or normalize(selected.get("seoToken")) != tokens[-1]:
            raise ScrapeError("Coles' configured frozen seafood category was not found in its taxonomy.")
        return selected

    def _browse_public_api(self):
        """Traverse the configured category through Coles' anonymous storefront APIs."""
        store_id = self._resolve_store_id()
        category = self._resolve_category(store_id)
        found = {}
        source_ids = set()
        expected_total = None
        api_page_size = None

        for page in range(self.max_pages):
            payload = self._api_get(
                "/products/search",
                {
                    "storeId": store_id,
                    # Despite its name, Coles' public ``start`` parameter is a
                    # zero-based page index. The response's ``pageSize`` is
                    # still an item count used to determine the final page.
                    "start": page,
                    "sortBy": "recommendedDescending",
                    "categoryId": category["id"],
                    "categoryLevel": category["level"],
                    "categoryName": category["name"],
                },
            )
            results = payload.get("results") or []
            if not results:
                break
            if expected_total is None:
                expected_total = int(payload.get("noOfResults") or 0)
                api_page_size = int(payload.get("pageSize") or 20)
                expected_pages = ((expected_total + api_page_size - 1) // api_page_size
                                  if expected_total else None)
                if expected_pages and expected_pages > self.max_pages:
                    raise ScrapeError(
                        f"Coles category requires {expected_pages} API pages, exceeding "
                        f"the configured limit of {self.max_pages}."
                    )
            for raw in results:
                if not isinstance(raw, dict):
                    continue
                raw_id = normalize(raw.get("id") or raw.get("productId") or raw.get("code"))
                if raw_id:
                    source_ids.add(raw_id)
                product_id, product = self._product(raw)
                if (product_id and product.get("category_group") and
                        is_allowed_product(product["name"], product["brand"],
                                           product["category_group"])):
                    found["coles:" + product_id] = product
            if (expected_total is not None and
                    (page + 1) * api_page_size >= expected_total):
                break
            time.sleep(self.delay)

        if expected_total and len(source_ids) < expected_total:
            raise ScrapeError(
                f"Coles API pagination was incomplete: received {len(source_ids)} "
                f"of {expected_total} products. The snapshot was not replaced."
            )
        return found

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
        page_props = payload.get("pageProps") or payload.get("props", {}).get("pageProps", {})
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
        image_urls = []
        for image in images:
            candidate = image.get("uri", "") if isinstance(image, dict) else str(image)
            if candidate.startswith("/"):
                candidate = "https://cdn.productimages.coles.com.au/productimages" + candidate
            if candidate and candidate not in image_urls:
                image_urls.append(candidate)
        uri = raw.get("imageUrl") or raw.get("image_url") or (image_urls[0] if image_urls else "")
        if uri.startswith("/"):
            uri = "https://cdn.productimages.coles.com.au/productimages" + uri
        if uri and uri not in image_urls:
            image_urls.insert(0, uri)
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
        # Coles does not consistently nest its promotion copy under ``pricing``.
        # Search pricing first (the usual shape), then the complete product so
        # offers exposed in badges/promotions are not silently dropped.
        multibuy_text = find_multibuy_text(pricing) or find_multibuy_text(raw)
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
        merchandise_hierarchy = raw.get("merchandiseHeir") or {}
        online_hierarchies = raw.get("onlineHeirs") or []
        category_hint = " ".join(
            normalize(value)
            for value in (
                merchandise_hierarchy.get("categoryGroup"),
                merchandise_hierarchy.get("category"),
                merchandise_hierarchy.get("subCategory"),
                merchandise_hierarchy.get("className"),
                *(hierarchy.get("aisle") for hierarchy in online_hierarchies
                  if isinstance(hierarchy, dict)),
            )
        )
        group = category_group(name, category_hint)
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
            "size": size, "image_url": uri, "image_urls": image_urls,
            "category_group": group,
            "online_only": bool(pricing.get("onlineSpecial")) or
                           "ONLINE" in normalize(pricing.get("promotionType")).upper(),
            "product_url": product_url, "source": product_url,
        }

    def _browse_page_data(self):
        """Read every page of the configured Coles category."""
        parsed = urlparse(self.category_url)
        category_path = parsed.path or "/browse/frozen/frozen-fish-seafood"
        base_params = dict(parse_qsl(parsed.query))
        base_params.setdefault("sortBy", "recommendedDescending")
        if self.location.get("postcode"):
            base_params["postcode"] = self.location["postcode"]
        if self.location.get("state"):
            base_params["state"] = self.location["state"]
        if self.location.get("context_mode"):
            base_params["contextMode"] = self.location["context_mode"]

        found = {}
        source_ids = set()
        expected_total = None
        expected_pages = None
        for page in range(1, self.max_pages + 1):
            params = dict(base_params)
            if page > 1:
                params["page"] = page
            url = BASE_URL + category_path + "?" + urlencode(params)
            response = self._get(
                url,
                headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            )
            script = BeautifulSoup(response.text, "html.parser").find("script", id="__NEXT_DATA__")
            if script is not None and script.string:
                payload = json.loads(script.string)
            else:
                # GitHub-hosted runners have intermittently challenged one of
                # Coles' two equivalent public page-data routes. Try the page's
                # Next data representation before retaining the old snapshot.
                build_id = self.discover_build_id()
                data_path = "/en" + category_path + ".json"
                data_url = (f"{BASE_URL}/_next/data/{quote(build_id, safe='')}"
                            f"{data_path}?{urlencode(params)}")
                try:
                    data_response = self._get(data_url)
                except requests.RequestsError as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status != 404:
                        raise
                    build_id = self.discover_build_id(force=True)
                    data_url = (f"{BASE_URL}/_next/data/{quote(build_id, safe='')}"
                                f"{data_path}?{urlencode(params)}")
                    data_response = self._get(data_url)
                if "json" not in data_response.headers.get("content-type", "").lower():
                    raise ScrapeError(
                        "Coles returned bot protection on both category data routes. "
                        "The last verified snapshot was retained."
                    )
                payload = data_response.json()
            results, metadata = self._find_results(payload)
            if not results:
                break
            if expected_total is None:
                expected_total = int(metadata.get("noOfResults") or metadata.get("totalResults") or
                                     metadata.get("total") or metadata.get("totalCount") or 0)
                returned_page_size = int(metadata.get("pageSize") or self.page_size)
                expected_pages = ((expected_total + returned_page_size - 1) // returned_page_size
                                  if expected_total else None)
                if expected_pages and expected_pages > self.max_pages:
                    raise ScrapeError(
                        f"Coles category requires {expected_pages} pages, exceeding the "
                        f"configured limit of {self.max_pages}."
                    )
            for raw in results:
                if not isinstance(raw, dict):
                    continue
                raw_id = str(raw.get("id") or raw.get("productId") or raw.get("code") or "").strip()
                if raw_id:
                    source_ids.add(raw_id)
                product_id, product = self._product(raw)
                if (product_id and product.get("category_group") and
                        is_allowed_product(product["name"], product["brand"],
                                           product["category_group"])):
                    found["coles:" + product_id] = product
            if expected_pages and page >= expected_pages:
                break
            time.sleep(self.delay)
        if expected_total and len(source_ids) < expected_total:
            raise ScrapeError(
                f"Coles category pagination was incomplete: received {len(source_ids)} "
                f"of {expected_total} products. The snapshot was not replaced."
            )
        return found

    def browse(self):
        """Prefer Coles' storefront API, retaining page-data as a compatibility fallback."""
        api_error = None
        try:
            products = self._browse_public_api()
            if products:
                return products
            api_error = ScrapeError("Coles' storefront API returned no matching products.")
        except (requests.RequestsError, ValueError, KeyError, TypeError, ScrapeError) as exc:
            api_error = exc

        try:
            return self._browse_page_data()
        except Exception as page_error:
            raise ScrapeError(
                "Coles' public storefront API and category page both failed. "
                f"API: {type(api_error).__name__}: {api_error}; "
                f"page: {type(page_error).__name__}: {page_error}"
            ) from page_error

    def search(self, query):
        build_id = self.discover_build_id()
        found = {}
        for page in range(1, self.max_pages + 1):
            params = {"q": query}
            if self.location.get("postcode"):
                params["postcode"] = self.location["postcode"]
            if self.location.get("state"):
                params["state"] = self.location["state"]
            if self.location.get("context_mode"):
                params["contextMode"] = self.location["context_mode"]
            if page > 1:
                params["page"] = page
            url = f"{BASE_URL}/_next/data/{quote(build_id, safe='')}/en/search/products.json?{urlencode(params)}"
            try:
                response = self._get(url)
            except requests.RequestsError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status == 404:
                    build_id = self.discover_build_id(force=True)
                    url = f"{BASE_URL}/_next/data/{quote(build_id, safe='')}/en/search/products.json?{urlencode(params)}"
                    response = self._get(url)
                else:
                    raise
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type.lower():
                raise ScrapeError("Coles returned a bot-protection page instead of product JSON. No report was generated.")
            results, metadata = self._find_results(response.json())
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
        return found

    def scrape(self, queries):
        products = self.browse()
        if not products:
            raise ScrapeError("Coles returned no matching products. Snapshot was not replaced.")
        missing = [pid for pid, p in products.items() if not p["name"] or not p["product_url"]]
        if missing:
            raise ScrapeError(f"Coles returned incomplete records for {len(missing)} products. Snapshot was not replaced.")
        return products

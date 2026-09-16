import time
import requests
from typing import List, Dict, Any

from config import CLIENT_ID, CLIENT_SECRET, PARTNER_TAG

# --- Endpoints (set to your real docs values) ---
TOKEN_URL = "https://api.amazon.co.uk/auth/o2/token"
# Stub: replace with real keyword-search endpoint from your Creators API docs
# SEARCH_ITEMS_URL = "https://creatorsapi.amazon" 
SEARCH_ITEMS_URL = "https://creatorsapi.amazon/catalog/v1/searchItems"
# Confirmed by your curl
GET_ITEMS_URL = "https://creatorsapi.amazon/catalog/v1/getItems"
DEFAULT_MARKETPLACE = "www.amazon.de"
_cached_token = None
_token_expires_at = 0.0


class AmazonApiError(Exception):
    pass


def get_access_token() -> str:
    global _cached_token, _token_expires_at

    now = time.time()
    if _cached_token and (_token_expires_at - now) > 60:
        return _cached_token

    payload = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": "creatorsapi::default",
    }
    headers = {"Content-Type": "application/json"}

    try:
        resp = requests.post(TOKEN_URL, json=payload, headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        raise AmazonApiError(f"Token request failed: {e}") from e
    except ValueError as e:
        raise AmazonApiError(f"Token response not JSON: {e}") from e

    token = data.get("access_token")
    if not token:
        raise AmazonApiError(f"No access_token in token response: {data}")

    expires_in = int(data.get("expires_in", 3600))
    _cached_token = token
    _token_expires_at = now + expires_in
    return token


def _auth_headers(token: str, marketplace: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "x-marketplace": marketplace,
    }


def search_items_by_keyword(keyword: str, marketplace: str = DEFAULT_MARKETPLACE) -> Dict[str, Any]:
    """
    Step 1: Keyword search -> returns candidate items/ASINs
    NOTE: endpoint/payload may differ in your API docs; adjust accordingly.
    """
    if not keyword or not keyword.strip():
        raise AmazonApiError("Keyword must not be empty.")

    token = get_access_token()
    headers = _auth_headers(token, marketplace)

    payload = {
        "keywords": keyword.strip(),
        "marketplace": marketplace,
        "partnerTag": PARTNER_TAG,
        "searchIndex": "Books",
        # "condition": "Collectible",
        "resources": [
            "itemInfo.title",
            "images.primary.small",
            "parentASIN",
        ],
    }

    try:
        resp = requests.post(SEARCH_ITEMS_URL, json=payload, headers=headers, timeout=30)
    except requests.RequestException as e:
        raise AmazonApiError(f"searchItems request failed: {e}") from e

    if resp.status_code != 200:
        raise AmazonApiError(f"searchItems failed ({resp.status_code}): {resp.text[:800]}")

    try:
        return resp.json()
    except ValueError as e:
        raise AmazonApiError(f"searchItems response not JSON: {e}") from e


def get_items_by_asin(item_ids: List[str], marketplace: str = DEFAULT_MARKETPLACE) -> Dict[str, Any]:
    """
    Step 2: getItems by ASIN -> rich item details
    """
    if not item_ids:
        raise AmazonApiError("item_ids must not be empty.")

    token = get_access_token()
    headers = _auth_headers(token, marketplace)

    payload = {
        "itemIds": item_ids,
        "itemIdType": "ASIN",
        "marketplace": marketplace,
        "partnerTag": PARTNER_TAG,
        "resources": [
            "images.primary.small",
            "itemInfo.title",
            "itemInfo.features",
            "parentASIN",
        ],
    }

    try:
        resp = requests.post(GET_ITEMS_URL, json=payload, headers=headers, timeout=30)
    except requests.RequestException as e:
        raise AmazonApiError(f"getItems request failed: {e}") from e

    if resp.status_code != 200:
        raise AmazonApiError(f"getItems failed ({resp.status_code}): {resp.text[:800]}")

    try:
        return resp.json()
    except ValueError as e:
        raise AmazonApiError(f"getItems response not JSON: {e}") from e


def _extract_asins(search_json: Dict[str, Any], max_items: int = 10) -> List[str]:
    """
    Best-effort extraction because response shape can vary by product version.
    """
    asins = []
    candidates = []

    if isinstance(search_json, dict):
        # common patterns
        candidates = (
            search_json.get("items")
            or search_json.get("searchResult", {}).get("items")
            or search_json.get("data", {}).get("items")
            or []
        )

    for item in candidates:
        if not isinstance(item, dict):
            continue
        asin = item.get("asin") or item.get("ASIN") or item.get("itemId")
        if asin and isinstance(asin, str):
            asins.append(asin)

    # de-dup keep order
    uniq = []
    seen = set()
    for a in asins:
        if a not in seen:
            seen.add(a)
            uniq.append(a)

    return uniq[:max_items]


def lookup_by_keyword(keyword: str, marketplace: str = DEFAULT_MARKETPLACE) -> Dict[str, Any]:
    """
    Full flow:
      keyword -> searchItems -> ASINs -> getItems
    Returns combined debug-friendly payload.
    """
    search_json = search_items_by_keyword(keyword, marketplace=marketplace)
    asins = _extract_asins(search_json)

    if not asins:
        return {
            "ok": False,
            "keyword": keyword,
            "marketplace": marketplace,
            "message": "No ASINs found from searchItems result.",
            "search_raw": search_json,
            "items_raw": None,
        }

    items_json = get_items_by_asin(asins, marketplace=marketplace)
    return {
        "ok": True,
        "keyword": keyword,
        "marketplace": marketplace,
        "asins": asins,
        "search_raw": search_json,
        "items_raw": items_json,
    }


if __name__ == "__main__":
    try:
        test_keyword = "Michael Ende Die unendliche Geschichte Stuttgart"
        result = lookup_by_keyword(test_keyword, marketplace="www.amazon.de")
        print("OK")
        print(result)
    except Exception as e:
        print(f"ERROR: {e}")

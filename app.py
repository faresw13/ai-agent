from flask import Flask, request, jsonify
import os
import json
import urllib.request
import urllib.error
import re
from urllib.parse import urljoin
import psycopg

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

BACKEND_ORIGINS = "*"


# =========================================================
# CORS
# =========================================================

@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = BACKEND_ORIGINS
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, Authorization"
    )
    response.headers["Access-Control-Allow-Methods"] = (
        "GET, POST, OPTIONS"
    )
    return response


@app.route("/<path:path>", methods=["OPTIONS"])
@app.route("/", methods=["OPTIONS"])
def options(path=None):
    return "", 204


# =========================================================
# Database
# =========================================================

def get_db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")

    return psycopg.connect(DATABASE_URL)


def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS salla_tokens (
                    merchant_id TEXT PRIMARY KEY,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT,
                    expires BIGINT
                )
            """)
        conn.commit()


# =========================================================
# Helpers
# =========================================================

def salla_headers(access_token):
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/152.0.0.0 Safari/537.36"
        ),
        "Accept-Language": (
            "ar-SA,ar;q=0.9,en-US;q=0.8,en;q=0.7"
        )
    }


def get_connected_store():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT merchant_id, access_token
                FROM salla_tokens
                LIMIT 1
            """)

            return cur.fetchone()


def get_store_info(access_token):
    req = urllib.request.Request(
        "https://api.salla.dev/admin/v2/store/info",
        headers=salla_headers(access_token)
    )

    with urllib.request.urlopen(req, timeout=timeout) as response:
        result = json.loads(
            response.read().decode("utf-8")
        )

    return result.get("data", {})


def salla_get_json(access_token, url):
    req = urllib.request.Request(
        url,
        headers=salla_headers(access_token),
        method="GET"
    )

    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(
            response.read().decode("utf-8")
        )


def get_salla_collection(access_token, endpoint, per_page=100):
    all_items = []
    page = 1

    while True:
        separator = "&" if "?" in endpoint else "?"
        url = (
            f"https://api.salla.dev/admin/v2/{endpoint}"
            f"{separator}page={page}&per_page={per_page}"
        )

        result = salla_get_json(access_token, url)

        items = result.get("data", [])
        if not isinstance(items, list):
            items = []

        all_items.extend(items)

        pagination = result.get("pagination") or {}
        total_pages = pagination.get("totalPages")

        if not total_pages or page >= int(total_pages):
            break

        page += 1

        # Safety limit so a bad pagination response cannot loop forever.
        if page > 100:
            break

    return all_items


def get_products(access_token):
    # "format=light" keeps the response smaller while still giving
    # the agent the main product information needed for analysis.
    return get_salla_collection(
        access_token,
        "products?format=light"
    )


def get_product_details(access_token, product_id):
    if not product_id:
        raise ValueError("product_id is required")
    return salla_get_json(
        access_token,
        f"https://api.salla.dev/admin/v2/products/{product_id}"
    )


def get_categories(access_token):
    return get_salla_collection(
        access_token,
        "categories"
    )


def extract_openai_text(result):
    if result.get("output_text"):
        return result["output_text"]

    texts = []

    for item in result.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                text = content.get("text")

                if text:
                    texts.append(text)

    if texts:
        return "\n".join(texts)

    return "ما قدرت أستخرج رد الذكاء الاصطناعي."


# =========================================================
# Basic routes
# =========================================================

@app.route("/", methods=["GET"])
def home():
    return "AI Agent is running"


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok"
    }), 200


# =========================================================
# Salla Webhook
# =========================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True) or {}

    event = data.get("event")
    merchant = data.get("merchant")

    print(
        f"Salla webhook received: "
        f"event={event}, merchant={merchant}"
    )

    if event == "app.store.authorize":
        payload = data.get("data", {})

        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        expires = payload.get("expires")

        if access_token and merchant:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO salla_tokens
                        (
                            merchant_id,
                            access_token,
                            refresh_token,
                            expires
                        )
                        VALUES (%s, %s, %s, %s)

                        ON CONFLICT (merchant_id)
                        DO UPDATE SET
                            access_token = EXCLUDED.access_token,
                            refresh_token = EXCLUDED.refresh_token,
                            expires = EXCLUDED.expires
                    """, (
                        str(merchant),
                        access_token,
                        refresh_token,
                        expires
                    ))

                conn.commit()

            print(
                f"Salla authorization saved "
                f"for merchant={merchant}"
            )

    elif event == "app.uninstalled":
        if merchant:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM salla_tokens
                        WHERE merchant_id = %s
                        """,
                        (str(merchant),)
                    )

                conn.commit()

            print(
                f"Salla authorization removed "
                f"for merchant={merchant}"
            )

    return jsonify({
        "success": True
    }), 200


# =========================================================
# Connected stores
# =========================================================

@app.route("/status", methods=["GET"])
def status():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT merchant_id
                FROM salla_tokens
            """)

            merchants = [
                row[0]
                for row in cur.fetchall()
            ]

    return jsonify({
        "connected_merchants": merchants
    }), 200


# =========================================================
# Test Salla connection
# =========================================================

@app.route("/test-salla", methods=["GET"])
def test_salla():
    row = get_connected_store()

    if not row:
        return jsonify({
            "success": False,
            "message": "No authorized Salla store"
        }), 404

    merchant, access_token = row

    try:
        store = get_store_info(access_token)

        return jsonify({
            "success": True,
            "store_id": store.get("id"),
            "store_name": store.get("name")
        }), 200

    except Exception as e:
        print(
            f"Salla API error: "
            f"{type(e).__name__}"
        )

        return jsonify({
            "success": False,
            "message": "Salla API request failed"
        }), 500


# =========================================================
# AI AGENT - Store Information
# =========================================================

@app.route("/agent/store-info", methods=["GET"])
def agent_store_info():
    row = get_connected_store()

    if not row:
        return jsonify({
            "success": False,
            "message": "No authorized Salla store"
        }), 404

    merchant_id, access_token = row

    try:
        store = get_store_info(access_token)

        return jsonify({
            "success": True,
            "merchant_id": merchant_id,
            "store": store
        }), 200

    except Exception as e:
        print(
            f"Agent store info error: "
            f"{type(e).__name__}"
        )

        return jsonify({
            "success": False,
            "message": (
                "Failed to read "
                "Salla store information"
            )
        }), 500


# =========================================================
# AI AGENT - Salla Tools
# =========================================================

def salla_request(access_token, method, endpoint, body=None, timeout=30):
    url = endpoint
    if not endpoint.startswith("http"):
        url = f"https://api.salla.dev/admin/v2{endpoint}"

    headers = salla_headers(access_token)
    headers["Content-Type"] = "application/json"

    data = None
    if body is not None:
        data = json.dumps(
            body,
            ensure_ascii=False
        ).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method
    )

    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8")

    if not raw:
        return {}

    return json.loads(raw)


def salla_get_json(access_token, url):
    return salla_request(
        access_token,
        "GET",
        url
    )


def get_salla_collection(access_token, endpoint, per_page=100):
    all_items = []
    page = 1

    while True:
        separator = "&" if "?" in endpoint else "?"
        url = (
            f"{endpoint}{separator}"
            f"page={page}&per_page={per_page}"
        )

        result = salla_get_json(
            access_token,
            f"https://api.salla.dev/admin/v2/{url}"
        )

        items = result.get("data", [])
        if not isinstance(items, list):
            items = []

        all_items.extend(items)

        pagination = result.get("pagination") or {}
        total_pages = pagination.get("totalPages")

        if not total_pages:
            break

        if page >= int(total_pages):
            break

        page += 1

        if page > 100:
            break

    return all_items


def create_category(access_token, arguments):
    allowed = {
        key: value
        for key, value in arguments.items()
        if value is not None
    }

    return salla_request(
        access_token,
        "POST",
        "/categories",
        allowed
    )


def update_category(access_token, arguments):
    category_id = arguments.get("category_id")

    if not category_id:
        raise ValueError("category_id is required")

    body = {
        key: value
        for key, value in arguments.items()
        if key != "category_id" and value is not None
    }

    return salla_request(
        access_token,
        "PUT",
        f"/categories/{category_id}",
        body
    )


def _multipart_field(boundary, name, value):
    return (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"{name}\"\r\n\r\n"
        f"{value}\r\n"
    )


def _multipart_file(boundary, name, filename, content_type, data):
    header = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    return header + data + b"\r\n"


def _download_image_bytes(image_url, timeout=12):
    req = urllib.request.Request(
        image_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/152.0.0.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
        }
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].lower()
        data = response.read(8 * 1024 * 1024)
        final_url = response.geturl()

    if not data:
        raise ValueError("image download returned no bytes")

    # Some CDNs omit Content-Type. Infer a safe extension from the URL.
    lowered = final_url.lower().split("?", 1)[0]
    if content_type == "image/jpeg" or lowered.endswith((".jpg", ".jpeg")):
        content_type = "image/jpeg"
        ext = ".jpg"
    elif content_type == "image/png" or lowered.endswith(".png"):
        content_type = "image/png"
        ext = ".png"
    elif content_type == "image/webp" or lowered.endswith(".webp"):
        content_type = "image/webp"
        ext = ".webp"
    elif content_type == "image/avif" or lowered.endswith(".avif"):
        # Salla can be stricter with AVIF uploads; convert is not available here,
        # so keep URL mode as the primary path and only use this as a last resort.
        ext = ".avif"
    else:
        raise ValueError(f"downloaded resource is not an image: {content_type or 'unknown'}")

    return data, content_type, f"product-image{ext}", final_url


def _post_product_image_multipart(access_token, product_id, fields, file_part=None, timeout=20):
    boundary = "----FaresAIAgentBoundaryV8X9pQ"
    body_parts = []
    for key, value in fields.items():
        if value is not None:
            body_parts.append(_multipart_field(boundary, key, value).encode("utf-8"))

    if file_part:
        filename, content_type, data = file_part
        body_parts.append(
            _multipart_file(boundary, "photo", filename, content_type, data)
        )

    body_parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(body_parts)

    req = urllib.request.Request(
        f"https://api.salla.dev/admin/v2/products/{product_id}/images",
        data=body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "Fares-AI-Agent/1.0"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        error_body = ""
        try:
            error_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(
            f"Salla image upload failed HTTP {exc.code}: {error_body[:1200]}"
        ) from exc


def attach_product_image(access_token, product_id, image_url, alt_text=""):
    """Attach a public image URL, with a file-upload fallback for Salla 422 responses."""
    if not image_url:
        raise ValueError("image_url is required")

    fields = {
        "original": str(image_url),
        "main": "true",
        "default": "1",
        "sort": "1",
        "alt": alt_text or "product image"
    }

    # First: use Salla's documented URL-link mode.
    try:
        return _post_product_image_multipart(
            access_token, product_id, fields, file_part=None, timeout=20
        )
    except RuntimeError as first_error:
        print("IMAGE URL ATTACH FAILED:", str(first_error))

        # Second: download the real image ourselves and upload it as `photo`.
        # This avoids cases where Salla cannot fetch an external CDN URL.
        try:
            data, content_type, filename, final_url = _download_image_bytes(
                image_url, timeout=12
            )
            file_result = _post_product_image_multipart(
                access_token,
                product_id,
                fields={
                    "original": final_url,
                    "main": "true",
                    "default": "1",
                    "sort": "1",
                    "alt": alt_text or "product image"
                },
                file_part=(filename, content_type, data),
                timeout=20
            )
            file_result["_upload_mode"] = "photo_file_fallback"
            return file_result
        except Exception as second_error:
            print("IMAGE FILE FALLBACK FAILED:", repr(second_error))
            raise RuntimeError(
                f"{first_error}; file fallback failed: {second_error}"
            ) from second_error


def _collect_urls(value):
    urls = []

    if isinstance(value, dict):
        for key, item in value.items():
            if key == "url" and isinstance(item, str):
                if item.startswith(("http://", "https://")):
                    urls.append(item)
            urls.extend(_collect_urls(item))
    elif isinstance(value, list):
        for item in value:
            urls.extend(_collect_urls(item))
    elif isinstance(value, str):
        urls.extend(re.findall(r"https?://[^\\s<>\\\"]+", value))

    return urls


def _extract_page_image_url(page_url, timeout=15):
    """Fetch a public page and extract og:image/twitter:image/JSON-LD image."""
    from html import unescape

    req = urllib.request.Request(
        page_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/152.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml"
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type", "")
            final_url = response.geturl()
            raw = response.read(600000).decode("utf-8", errors="ignore")
    except Exception:
        return None

    if content_type.lower().startswith("image/"):
        return final_url

    patterns = [
        r'<meta[^>]+property=["\\\']og:image["\\\'][^>]+content=["\\\']([^"\\\']+)["\\\']',
        r'<meta[^>]+name=["\\\']twitter:image["\\\'][^>]+content=["\\\']([^"\\\']+)["\\\']',
        r'<meta[^>]+property=["\\\']og:image:url["\\\'][^>]+content=["\\\']([^"\\\']+)["\\\']',
        r'<meta[^>]+content=["\\\']([^"\\\']+)["\\\'][^>]+property=["\\\']og:image["\\\']',
        r'<meta[^>]+content=["\\\']([^"\\\']+)["\\\'][^>]+name=["\\\']twitter:image["\\\']'
    ]

    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.I)
        if match:
            candidate = unescape(match.group(1)).strip()
            if candidate.startswith("//"):
                candidate = "https:" + candidate
            elif candidate.startswith("/"):
                candidate = urljoin(final_url, candidate)
            if candidate.startswith(("http://", "https://")):
                return candidate

    # JSON-LD product/image data.
    for block in re.findall(
        r'<script[^>]+type=["\\\']application/ld\\+json["\\\'][^>]*>(.*?)</script>',
        raw,
        flags=re.I | re.S
    ):
        try:
            data = json.loads(unescape(block))
        except Exception:
            continue

        def walk_image(obj):
            if isinstance(obj, dict):
                image = obj.get("image")
                if isinstance(image, str) and image.startswith(("http://", "https://")):
                    return image
                if isinstance(image, dict):
                    value = image.get("url")
                    if isinstance(value, str) and value.startswith(("http://", "https://")):
                        return value
                if isinstance(image, list):
                    for item in image:
                        value = walk_image(item)
                        if value:
                            return value
                for child in obj.values():
                    value = walk_image(child)
                    if value:
                        return value
            elif isinstance(obj, list):
                for child in obj:
                    value = walk_image(child)
                    if value:
                        return value
            return None

        candidate = walk_image(data)
        if candidate:
            return candidate

    return None


def _allowed_domains_for_product(product_name):
    """Prefer official manufacturer sites when the brand is recognizable."""
    name = (product_name or "").lower()
    mapping = {
        "apple": ["apple.com"],
        "iphone": ["apple.com"],
        "ipad": ["apple.com"],
        "macbook": ["apple.com"],
        "airpods": ["apple.com"],
        "samsung": ["samsung.com"],
        "galaxy": ["samsung.com"],
        "sony": ["sony.com"],
        "playstation": ["playstation.com"],
        "ps5": ["playstation.com"],
        "xbox": ["xbox.com", "microsoft.com"],
        "microsoft": ["microsoft.com"],
        "google pixel": ["store.google.com", "google.com"],
        "pixel": ["store.google.com", "google.com"],
        "huawei": ["huawei.com"],
        "xiaomi": ["mi.com", "xiaomi.com"],
        "oneplus": ["oneplus.com"],
        "nintendo": ["nintendo.com"],
        "dyson": ["dyson.com"],
        "nike": ["nike.com"],
        "adidas": ["adidas.com"],
    }

    for keyword, domains in mapping.items():
        if keyword in name:
            return domains

    return []


def _extract_web_search_source_urls(result):
    """Extract URLs from web-search sources and from the model's returned text."""
    urls = []

    # Some Responses API variants expose search sources under the
    # web_search_call action. Keep these first because they are the
    # strongest provenance signal.
    for item in result.get("output", []) if isinstance(result, dict) else []:
        if item.get("type") != "web_search_call":
            continue

        action = item.get("action") or {}
        sources = action.get("sources") or []

        for source in sources:
            if not isinstance(source, dict):
                continue
            url = source.get("url")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                if url not in urls:
                    urls.append(url)

    # The web-search model can also return the selected page URL as text.
    # Parse URLs from output_text/content because not every API response
    # exposes the source list in the same shape.
    text = result.get("output_text") or ""
    if not text:
        chunks = []
        for item in result.get("output", []) if isinstance(result, dict) else []:
            for content in item.get("content", []) if isinstance(item, dict) else []:
                if content.get("type") == "output_text" and content.get("text"):
                    chunks.append(content["text"])
        text = "\n".join(chunks)

    for url in re.findall(r'https?://[^\s<>\"\']+', text):
        url = url.rstrip(".,;:)]}")
        if url not in urls:
            urls.append(url)

    # Final fallback for API variants that expose URLs in nested output data.
    for url in _collect_urls(result):
        if url not in urls:
            urls.append(url)

    return urls


def _is_blocked_source(url):
    lowered = (url or "").lower()
    blocked_parts = [
        "salla.sa",
        "salla.com",
        "localhost",
        "127.0.0.1",
        "demostore",
    ]
    return any(part in lowered for part in blocked_parts)


def _looks_like_image_url(image_url):
    """Fast heuristic for image URLs; Salla performs the final fetch/validation."""
    if not image_url or not image_url.startswith(("http://", "https://")):
        return False

    lowered = image_url.lower().split("?", 1)[0]
    image_exts = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".svg")
    if lowered.endswith(image_exts):
        return True

    image_markers = ("/image/", "/images/", "/img/", "/media/", "/assets/", "/product-images/", "/productimage")
    return any(marker in lowered for marker in image_markers)


def _validate_image_url(image_url, timeout=10):
    """Best-effort validation. Never reject a plausible CDN image only because HEAD is blocked."""
    if not image_url or not image_url.startswith(("http://", "https://")):
        return False

    try:
        req = urllib.request.Request(
            image_url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; FaresAIAgent/1.0)",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
            },
            method="HEAD"
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            if content_type.startswith("image/"):
                return True
    except Exception:
        pass

    try:
        req = urllib.request.Request(
            image_url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; FaresAIAgent/1.0)",
                "Range": "bytes=0-2048",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
            },
            method="GET"
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            response.read(2048)
            if content_type.startswith("image/"):
                return True
    except Exception:
        pass

    # Some legitimate CDN URLs block our HEAD/GET requests. If the URL itself
    # clearly looks like an image URL, let Salla be the final validator.
    return _looks_like_image_url(image_url)


def _image_search_queries(product_name):
    """Return several searches so one blocked manufacturer page cannot stop the agent."""
    name = product_name.strip()
    queries = [
        f'"{name}" direct image jpg png product photo',
        f'"{name}" product page image',
    ]

    lowered = name.lower()
    if any(k in lowered for k in ("iphone", "apple", "ipad", "macbook", "airpods")):
        queries.extend([
            f'"{name}" site:luluhypermarket.com image',
            f'"{name}" site:saco.sa image',
            f'"{name}" site:carrefourksa.com image',
        ])
    elif any(k in lowered for k in ("galaxy", "samsung")):
        queries.extend([
            f'"{name}" site:samsung.com image',
            f'"{name}" site:luluhypermarket.com image',
            f'"{name}" site:carrefourksa.com image',
        ])
    elif any(k in lowered for k in ("playstation", "ps5")):
        queries.extend([
            f'"{name}" site:playstation.com image',
            f'"{name}" site:luluhypermarket.com image',
            f'"{name}" site:carrefourksa.com image',
        ])

    return queries


def _direct_product_page_candidates(product_name):
    """Build a few deterministic official product-page candidates.
    These are page URLs, not image URLs; _extract_page_image_url resolves them.
    """
    name = (product_name or "").strip()
    lowered = name.lower()
    candidates = []

    if "iphone 17 pro" in lowered:
        candidates += [
            "https://www.apple.com/sa-ar/iphone-17-pro/",
            "https://www.apple.com/sa/iphone-17-pro/",
        ]
    elif "iphone 17" in lowered:
        candidates += [
            "https://www.apple.com/sa-ar/iphone-17/",
            "https://www.apple.com/sa/iphone-17/",
        ]
    elif "iphone" in lowered:
        candidates += [
            "https://www.apple.com/sa-ar/iphone/",
            "https://www.apple.com/sa/iphone/",
        ]

    return candidates


def search_product_image_with_openai(product_name):
    """Find a real public product image with a fast bounded strategy.

    Strategy:
    1) Try deterministic official product pages first (fast, no AI search).
    2) If needed, make ONE small OpenAI web-search call and use only direct
       image URLs returned by the search response.
    3) Do not fetch/validate many pages after the AI call; Salla is the final
       validator when attaching the image.
    """
    product_name = (product_name or "").strip()
    if not product_name:
        return {
            "success": False,
            "image_url": None,
            "alt": product_name,
            "message": "اسم المنتج غير موجود."
        }

    # Fast path: official pages for products with predictable URLs.
    for page_url in _direct_product_page_candidates(product_name)[:2]:
        if _is_blocked_source(page_url):
            continue
        image_url = _extract_page_image_url(page_url, timeout=5)
        if image_url and not _is_blocked_source(image_url):
            return {
                "success": True,
                "image_url": image_url,
                "alt": product_name,
                "source_page": page_url,
                "message": "تم العثور على صورة حقيقية من الصفحة الرسمية للمنتج."
            }

    if not OPENAI_API_KEY:
        return {
            "success": False,
            "image_url": None,
            "alt": product_name,
            "message": "OPENAI_API_KEY is not configured"
        }

    prompt = f"""
ابحث عن صورة حقيقية لهذا المنتج: {product_name}

أعد رابط صورة مباشر واحد فقط إن أمكن، ويجب أن يكون رابطًا يبدأ بـ http أو https
وينتهي بصورة أو يشير بوضوح إلى ملف صورة (jpg/jpeg/png/webp/avif).
الأولوية للموقع الرسمي للعلامة التجارية، ثم متجر موثوق.
لا تستخدم سلة، ولا تخترع رابطًا.
إذا لم تجد رابط صورة مباشرًا فلا تخترع أي رابط.
"""

    payload = {
        "model": "gpt-5.6-luna",
        "tools": [{
            "type": "web_search",
            "search_context_size": "low"
        }],
        "input": prompt
    }

    try:
        # Keep this bounded. The image tool must never hold the whole chat
        # request open long enough for a Render worker to be killed.
        result = openai_response(payload, timeout=18)
    except Exception as exc:
        print("IMAGE SEARCH ERROR:", repr(exc))
        return {
            "success": False,
            "image_url": None,
            "alt": product_name,
            "message": "تعذر البحث السريع عن صورة المنتج."
        }

    candidates = []
    text = result.get("output_text") or ""
    for url in re.findall(r'https?://[^\s<>\"\']+', text):
        url = url.rstrip(".,;:)]}")
        if not _is_blocked_source(url) and _looks_like_image_url(url):
            candidates.append(url)

    # Also inspect URLs exposed by the Responses API search result.
    for url in _extract_web_search_source_urls(result):
        if not _is_blocked_source(url) and _looks_like_image_url(url):
            candidates.append(url)

    seen = set()
    for image_url in candidates:
        if image_url in seen:
            continue
        seen.add(image_url)
        if _is_blocked_source(image_url):
            continue
        # Do not perform another network round trip here. Salla will validate
        # the URL when attach_product_image is called.
        return {
            "success": True,
            "image_url": image_url,
            "alt": product_name,
            "source_page": None,
            "message": "تم العثور على رابط صورة حقيقي وسيتم إرفاقه بالمنتج في سلة."
        }

    return {
        "success": False,
        "image_url": None,
        "alt": product_name,
        "message": "لم أجد رابط صورة مباشرًا صالحًا بسرعة كافية."
    }

def create_product(access_token, arguments):
    """Create a product, then find and attach a real public image when possible."""
    image_url = arguments.get("image_url")
    image_alt = arguments.get("image_alt") or arguments.get("name", "product image")

    body = {
        key: value
        for key, value in arguments.items()
        if key not in {"image_url", "image_alt"} and value is not None
    }

    body.setdefault("quantity", 10)
    body.setdefault("product_type", "product")
    body.setdefault("require_shipping", True)
    body.setdefault("status", "sale")

    created = salla_request(
        access_token,
        "POST",
        "/products",
        body
    )

    product_id = (created.get("data") or {}).get("id")
    if not product_id:
        raise RuntimeError("Salla created the product but did not return its ID")

    # If the model did not provide an image URL, search here as a safety net.
    # This guarantees image handling is part of product creation instead of
    # relying only on the model to call find_product_image first.
    search_info = None
    if not image_url:
        search_info = search_product_image_with_openai(arguments.get("name", ""))
        if search_info.get("success"):
            image_url = search_info.get("image_url")
            image_alt = search_info.get("alt") or image_alt

    if not image_url:
        return {
            "product": created,
            "image": {
                "success": False,
                "image_url": None,
                "message": (search_info or {}).get(
                    "message",
                    "تم إنشاء المنتج، لكن لم يتم العثور على صورة صالحة حتى الآن."
                )
            }
        }

    try:
        image_result = attach_product_image(
            access_token,
            product_id,
            image_url,
            image_alt
        )
        return {
            "product": created,
            "image": image_result,
            "image_source_page": (search_info or {}).get("source_page")
        }
    except Exception as first_error:
        print("First image attach failed:", repr(first_error))

        # Try one alternate image only. Never invent a URL.
        retry = search_product_image_with_openai(arguments.get("name", ""))
        retry_url = retry.get("image_url") if retry.get("success") else None

        if retry_url and retry_url != image_url:
            try:
                image_result = attach_product_image(
                    access_token,
                    product_id,
                    retry_url,
                    image_alt
                )
                return {
                    "product": created,
                    "image": image_result,
                    "image_source_page": retry.get("source_page"),
                    "image_retry": True
                }
            except Exception as retry_error:
                print("Second image attach failed:", repr(retry_error))

        # Product creation succeeded even if Salla rejected both image URLs.
        # Return a structured result instead of crashing /agent/chat with 500.
        return {
            "product": created,
            "image": {
                "success": False,
                "image_url": image_url,
                "message": "تم إنشاء المنتج، لكن سلة رفضت إرفاق الصورة. المنتج لم يُحذف."
            }
        }
    except Exception as exc:
        print("IMAGE ATTACH ERROR:", repr(exc))
        return {
            "product": created,
            "image": {
                "success": False,
                "image_url": image_url,
                "message": "تم إنشاء المنتج، لكن حدث خطأ أثناء إرفاق الصورة."
            }
        }

def update_product(access_token, arguments):
    product_id = arguments.get("product_id")

    if not product_id:
        raise ValueError("product_id is required")

    body = {
        key: value
        for key, value in arguments.items()
        if key != "product_id" and value is not None
    }

    return salla_request(
        access_token,
        "PUT",
        f"/products/{product_id}",
        body
    )


def simplify_products(products):
    result = []

    for product in products:
        result.append({
            "id": product.get("id"),
            "name": product.get("name"),
            "type": product.get("type"),
            "price": product.get("price"),
            "status": product.get("status"),
            "quantity": product.get("quantity"),
            "is_available": product.get("is_available"),
            "main_image": product.get("main_image"),
            "images": [
                {
                    "id": image.get("id"),
                    "url": image.get("url"),
                    "main": image.get("main"),
                    "alt": image.get("alt")
                }
                for image in (product.get("images") or [])
            ],
            "sku": product.get("sku"),
            "categories": [
                {
                    "id": category.get("id"),
                    "name": category.get("name")
                }
                for category in (product.get("categories") or [])
            ]
        })

    return result


def simplify_categories(categories):
    result = []

    for category in categories:
        result.append({
            "id": category.get("id"),
            "name": category.get("name"),
            "parent_id": category.get("parent_id"),
            "status": category.get("status"),
            "sort_order": category.get("sort_order")
        })

    return result


def get_agent_tools():
    return [
        {
            "type": "function",
            "name": "get_store_info",
            "description": (
                "اقرأ معلومات المتجر الحالية من سلة. "
                "استخدمها عندما تحتاج معرفة اسم المتجر أو حالته "
                "أو العملة أو الدومين."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False
            }
        },
        {
            "type": "function",
            "name": "get_products",
            "description": (
                "اقرأ منتجات المتجر الحالية من سلة. "
                "استخدمها قبل أي قرار يتعلق بالمنتجات."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False
            }
        },
        {
            "type": "function",
            "name": "get_categories",
            "description": (
                "اقرأ أقسام المتجر الحالية من سلة. "
                "استخدمها قبل إنشاء أو تعديل الأقسام."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False
            }
        },
        {
            "type": "function",
            "name": "create_category",
            "description": (
                "أنشئ قسمًا جديدًا في متجر سلة. "
                "استخدمه فقط عندما يطلب المستخدم إنشاء قسم "
                "أو عندما تكون الخطة واضحة ومطلوب تنفيذها."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "اسم القسم"
                    },
                    "status": {
                        "type": "string",
                        "enum": ["active", "hidden"]
                    },
                    "show_in": {
                        "type": "object",
                        "properties": {
                            "app": {"type": "boolean"},
                            "web": {"type": "boolean"}
                        },
                        "additionalProperties": False
                    },
                    "parent_id": {
                        "type": ["integer", "null"],
                        "description": "رقم القسم الأب إن وجد"
                    },
                    "metadata_title": {
                        "type": ["string", "null"]
                    },
                    "metadata_description": {
                        "type": ["string", "null"]
                    },
                    "metadata_url": {
                        "type": ["string", "null"]
                    }
                },
                "required": ["name"],
                "additionalProperties": False
            }
        },
        {
            "type": "function",
            "name": "update_category",
            "description": (
                "عدّل قسمًا موجودًا في متجر سلة."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category_id": {
                        "type": "integer",
                        "description": "رقم القسم"
                    },
                    "name": {
                        "type": ["string", "null"]
                    },
                    "status": {
                        "type": ["string", "null"],
                        "enum": ["active", "hidden", None]
                    },
                    "show_in": {
                        "type": ["object", "null"],
                        "properties": {
                            "app": {"type": "boolean"},
                            "web": {"type": "boolean"}
                        },
                        "additionalProperties": False
                    },
                    "metadata_title": {
                        "type": ["string", "null"]
                    },
                    "metadata_description": {
                        "type": ["string", "null"]
                    },
                    "metadata_url": {
                        "type": ["string", "null"]
                    }
                },
                "required": ["category_id"],
                "additionalProperties": False
            }
        },
        {
            "type": "function",
            "name": "find_product_image",
            "description": (
                "ابحث في الإنترنت عن صورة حقيقية مناسبة لمنتج محدد. "
                "استخدم هذه الأداة عندما يطلب المستخدم صورة من الإنترنت. "
                "لا ترجع رابط صفحة. ترجع فقط رابط صورة مباشر تم التحقق أنه يعيد Content-Type من نوع image/."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_name": {
                        "type": "string",
                        "description": "اسم المنتج"
                    }
                },
                "required": ["product_name"],
                "additionalProperties": False
            }
        },
        {
            "type": "function",
            "name": "create_product",
            "description": (
                "أنشئ منتجًا جديدًا في متجر سلة. "
                "يجب تحديد البيانات اللازمة مثل الاسم والسعر والكمية "
                "ونوع المنتج حسب الطلب."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string"
                    },
                    "price": {
                        "type": "number"
                    },
                    "quantity": {
                        "type": "number",
                        "description": "الكمية. إذا لم يحدد المستخدم كمية، استخدم 10."
                    },
                    "status": {
                        "type": "string",
                        "enum": ["sale", "out", "hidden"]
                    },
                    "product_type": {
                        "type": "string",
                        "enum": ["product", "service", "booking"],
                        "description": "نوع المنتج، والافتراضي product"
                    },
                    "description": {
                        "type": ["string", "null"]
                    },
                    "categories": {
                        "type": ["array", "null"],
                        "items": {"type": "integer"}
                    },
                    "sale_price": {
                        "type": ["number", "null"]
                    },
                    "cost_price": {
                        "type": ["number", "null"]
                    },
                    "require_shipping": {
                        "type": ["boolean", "null"]
                    },
                    "weight": {
                        "type": ["number", "null"]
                    },
                    "weight_type": {
                        "type": ["string", "null"]
                    },
                    "sku": {
                        "type": ["string", "null"]
                    },
                    "channels": {
                        "type": ["array", "null"],
                        "items": {"type": "string"}
                    },
                    "image_url": {
                        "type": ["string", "null"],
                        "description": "رابط صورة مباشر حقيقي أعادته أداة find_product_image"
                    },
                    "image_alt": {
                        "type": ["string", "null"],
                        "description": "النص البديل للصورة"
                    }
                },
                "required": [
                    "name",
                    "price"
                ],
                "additionalProperties": False
            }
        },
        {
            "type": "function",
            "name": "update_product",
            "description": (
                "عدّل منتجًا موجودًا في متجر سلة."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {
                        "type": "integer"
                    },
                    "name": {
                        "type": ["string", "null"]
                    },
                    "price": {
                        "type": ["number", "null"]
                    },
                    "quantity": {
                        "type": ["number", "null"]
                    },
                    "description": {
                        "type": ["string", "null"]
                    },
                    "categories": {
                        "type": ["array", "null"],
                        "items": {"type": "integer"}
                    },
                    "sale_price": {
                        "type": ["number", "null"]
                    },
                    "cost_price": {
                        "type": ["number", "null"]
                    },
                    "require_shipping": {
                        "type": ["boolean", "null"]
                    },
                    "weight": {
                        "type": ["number", "null"]
                    },
                    "weight_type": {
                        "type": ["string", "null"]
                    },
                    "sku": {
                        "type": ["string", "null"]
                    },
                    "status": {
                        "type": ["string", "null"],
                        "enum": ["sale", "out", "hidden", None]
                    },
                    "channels": {
                        "type": ["array", "null"],
                        "items": {"type": "string"}
                    }
                },
                "required": ["product_id"],
                "additionalProperties": False
            }
        }
    ]


def execute_agent_tool(name, arguments, access_token):
    if name == "get_store_info":
        return get_store_info(access_token)

    if name == "get_product_details":
        product_id = arguments.get("product_id")
        return get_product_details(access_token, product_id)

    if name == "get_products":
        return simplify_products(
            get_products(access_token)
        )

    if name == "get_categories":
        return simplify_categories(
            get_categories(access_token)
        )

    if name == "create_category":
        return create_category(
            access_token,
            arguments
        )

    if name == "update_category":
        return update_category(
            access_token,
            arguments
        )

    if name == "find_product_image":
        return search_product_image_with_openai(
            arguments.get("product_name", "")
        )

    if name == "create_product":
        return create_product(
            access_token,
            arguments
        )

    if name == "update_product":
        return update_product(
            access_token,
            arguments
        )

    raise ValueError(
        f"Unknown agent tool: {name}"
    )


def openai_response(payload, timeout=120):
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(
            payload,
            ensure_ascii=False
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        },
        method="POST"
    )

    with urllib.request.urlopen(
        req,
        timeout=timeout
    ) as response:
        return json.loads(
            response.read().decode("utf-8")
        )


def serialize_response_output(output):
    result = []

    for item in output or []:
        if hasattr(item, "model_dump"):
            result.append(item.model_dump())
        elif isinstance(item, dict):
            result.append(item)
        else:
            result.append(item)

    return result


# =========================================================
# AI AGENT - Read-only test routes
# =========================================================

@app.route("/agent/products", methods=["GET"])
def agent_products():
    row = get_connected_store()

    if not row:
        return jsonify({
            "success": False,
            "message": "No authorized Salla store"
        }), 404

    merchant_id, access_token = row

    try:
        products = get_products(access_token)

        return jsonify({
            "success": True,
            "merchant_id": merchant_id,
            "count": len(products),
            "products": products
        }), 200

    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8")
        except Exception:
            pass

        print(
            f"Salla products HTTP error: "
            f"{e.code} {error_body}"
        )

        return jsonify({
            "success": False,
            "message": "Failed to read Salla products"
        }), 500

    except Exception as e:
        print(
            f"Salla products error: "
            f"{type(e).__name__}: {e}"
        )

        return jsonify({
            "success": False,
            "message": "Failed to read Salla products"
        }), 500


@app.route("/agent/categories", methods=["GET"])
def agent_categories():
    row = get_connected_store()

    if not row:
        return jsonify({
            "success": False,
            "message": "No authorized Salla store"
        }), 404

    merchant_id, access_token = row

    try:
        categories = get_categories(access_token)

        return jsonify({
            "success": True,
            "merchant_id": merchant_id,
            "count": len(categories),
            "categories": categories
        }), 200

    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8")
        except Exception:
            pass

        print(
            f"Salla categories HTTP error: "
            f"{e.code} {error_body}"
        )

        return jsonify({
            "success": False,
            "message": "Failed to read Salla categories"
        }), 500

    except Exception as e:
        print(
            f"Salla categories error: "
            f"{type(e).__name__}: {e}"
        )

        return jsonify({
            "success": False,
            "message": "Failed to read Salla categories"
        }), 500


# =========================================================
# AI AGENT - Chat with Tool Calling
# =========================================================

@app.route("/agent/chat", methods=["POST"])
def agent_chat():

    if not OPENAI_API_KEY:
        return jsonify({
            "success": False,
            "message": "OPENAI_API_KEY is not configured"
        }), 500

    row = get_connected_store()

    if not row:
        return jsonify({
            "success": False,
            "message": "No authorized Salla store"
        }), 404

    merchant_id, access_token = row

    data = request.get_json(silent=True) or {}
    messages = data.get("messages", [])

    if not isinstance(messages, list):
        return jsonify({
            "success": False,
            "message": "messages must be an array"
        }), 400

    try:
        conversation = []

        for message in messages[-30:]:
            role = message.get("role")
            content = message.get("content", "")

            if role not in ["user", "assistant"]:
                continue

            if not content:
                continue

            conversation.append({
                "role": role,
                "content": content
            })

        system_instructions = """
أنت Fares AI، وكيل ذكي يعمل داخل متجر سلة.

أنت الآن Agent حقيقي، ولديك أدوات تستطيع بها قراءة وتعديل
المنتجات والأقسام في متجر سلة.

قواعد مهمة جدًا:

1. افهم طلب المستخدم أولًا.
2. إذا احتجت معرفة حالة المتجر، استخدم أدوات القراءة.
3. لا تخمّن أرقام المنتجات أو الأقسام؛ اقرأها من الأدوات.
4. إذا طلب المستخدم إنشاء أو تعديل منتج أو قسم، نفّذ الطلب باستخدام
   الأداة المناسبة.
5. بعد أي عملية إنشاء أو تعديل مهمة، استخدم أداة القراءة المناسبة
   للتحقق من الحالة الجديدة قبل أن تقول للمستخدم إن العملية تمت.
6. لا تقل "تم" إذا فشلت الأداة.
7. إذا رجعت الأداة بخطأ صلاحيات أو تحقق، وضّح الخطأ للمستخدم باختصار
   ولا تدّعي نجاح العملية.
8. لا تحذف أي شيء؛ لا توجد لديك أدوات حذف.
9. لا تغيّر أشياء لم يطلبها المستخدم.
10. إذا كان الطلب غامضًا أو يحتاج معلومة أساسية غير موجودة، اسأل
    المستخدم بدل التخمين.
11. إذا طلب المستخدم صورة من الإنترنت، يجب أولًا استدعاء أداة find_product_image والانتظار لنتيجتها.
12. لا تستخدم رابط صفحة ويب كرابط صورة ولا تخمّن أي رابط. استخدم image_url الذي أعادته الأداة فقط.
13. إذا فشلت find_product_image، لا تدّعي أن الصورة أضيفت.
14. إذا نجحت find_product_image، مرّر image_url وimage_alt إلى create_product. أداة create_product هي التي تحاول إرفاق الصورة فعليًا في سلة، وقد تعيد المحاولة بصورة بديلة إذا رفض المصدر الأول.
15. بعد إنشاء المنتج، استخدم get_product_details باستخدام product_id الذي أعادته create_product للتحقق من وجود المنتج والصورة. لا تستخدم get_products للتحقق مباشرة بعد الإنشاء.
16. تحدث بالعربية السعودية وبأسلوب واضح ومباشر.
17. يمكنك تنفيذ عدة عمليات متتالية إذا كان طلب المستخدم واضحًا.
18. قبل إنشاء قسم جديد، اقرأ الأقسام الحالية لتجنب التكرار.
19. قبل تعديل منتج، اقرأ المنتجات الحالية لتحديد المنتج الصحيح.
20. لا تعتبر مجرد اقتراح خطة تنفيذًا؛ استخدم الأدوات فعليًا عندما يطلب المستخدم التنفيذ.

عند طلب إنشاء منتج مع صورة من الإنترنت، اتبع هذا التسلسل: قراءة الأقسام عند الحاجة، البحث عن الصورة الحقيقية، إنشاء المنتج بالوصف والسعر والكمية والصورة، ثم قراءة المنتجات للتحقق من النتيجة.

هدفك أن تساعد صاحب المتجر على تجهيز متجره فعليًا، وليس فقط إعطائه
اقتراحات نظرية.
"""

        payload = {
            "model": "gpt-5.6-luna",
            "instructions": system_instructions,
            "input": conversation,
            "tools": get_agent_tools(),
            "tool_choice": "auto"
        }

        # Allow several tool calls in one user request.
        for _ in range(8):
            result = openai_response(payload)

            output = result.get("output", [])
            function_calls = [
                item for item in output
                if item.get("type") == "function_call"
            ]

            if not function_calls:
                ai_text = extract_openai_text(result)

                return jsonify({
                    "success": True,
                    "merchant_id": merchant_id,
                    "message": ai_text
                }), 200

            # Preserve the model's output items for the next Responses call.
            next_input = list(
                payload.get("input", [])
            )
            next_input.extend(
                serialize_response_output(output)
            )

            for call in function_calls:
                tool_name = call.get("name")
                call_id = call.get("call_id")
                raw_arguments = call.get("arguments") or "{}"

                try:
                    arguments = json.loads(raw_arguments)

                    print(
                        f"Agent tool call: "
                        f"{tool_name} {json.dumps(arguments, ensure_ascii=False)}"
                    )

                    tool_result = execute_agent_tool(
                        tool_name,
                        arguments,
                        access_token
                    )

                    tool_output = {
                        "success": True,
                        "result": tool_result
                    }

                except urllib.error.HTTPError as e:
                    error_body = ""

                    try:
                        error_body = (
                            e.read()
                            .decode("utf-8")
                        )
                    except Exception:
                        pass

                    print(
                        f"Salla tool HTTP error: "
                        f"{tool_name} {e.code} {error_body}"
                    )

                    tool_output = {
                        "success": False,
                        "error": (
                            f"Salla API returned HTTP {e.code}"
                        ),
                        "details": error_body[:3000]
                    }

                except Exception as e:
                    print(
                        f"Agent tool error: "
                        f"{tool_name} "
                        f"{type(e).__name__}: {e}"
                    )

                    tool_output = {
                        "success": False,
                        "error": str(e)
                    }

                next_input.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(
                        tool_output,
                        ensure_ascii=False
                    )
                })

            payload = {
                "model": "gpt-5.6-luna",
                "instructions": system_instructions,
                "input": next_input,
                "tools": get_agent_tools(),
                "tool_choice": "auto"
            }

        return jsonify({
            "success": False,
            "message": (
                "وصلت لحد العمليات المتتالية المسموح به. "
                "جرّب تقسيم الطلب إلى خطوات."
            )
        }), 500

    except urllib.error.HTTPError as e:
        error_body = ""

        try:
            error_body = (
                e.read()
                .decode("utf-8")
            )
        except Exception:
            pass

        print(
            f"OpenAI HTTP error: "
            f"{e.code} {error_body}"
        )

        return jsonify({
            "success": False,
            "message": "AI request failed"
        }), 500

    except Exception as e:
        print(
            f"AI chat error: "
            f"{type(e).__name__}: {e}"
        )

        return jsonify({
            "success": False,
            "message": "AI request failed"
        }), 500


# =========================================================
# Start application
# =========================================================

init_db()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=10000
    )

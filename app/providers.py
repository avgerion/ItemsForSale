from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any
import uuid

from PIL import Image, ImageOps


class ProviderError(Exception):
    pass


logger = logging.getLogger(__name__)
ProviderPhoto = tuple[str, bytes, str]

_ebay_token: tuple[str, float] | None = None
_ebay_token_lock = threading.Lock()
_API_TIMEOUT_SECONDS = 12
_MAX_VISION_PHOTOS = 4
_MAX_VISION_BYTES = 15 * 1024 * 1024
_MAX_VISION_ENCODED_BYTES = 12 * 1024 * 1024
_MAX_PROVIDER_ERROR_BYTES = 4096


def _provider_error_log_detail(payload: Any) -> str:
    if isinstance(payload, str):
        if len(payload) > _MAX_PROVIDER_ERROR_BYTES:
            return "error response body exceeded the logging limit"
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            text = re.sub(r"<[^>]*>", " ", payload)
            text = re.sub(r"\s+", " ", text).strip()
            return _redact_provider_error(text[:1000]) or "provider returned an empty error body"
    if isinstance(payload, bytes):
        if len(payload) > _MAX_PROVIDER_ERROR_BYTES:
            return "error response body exceeded the logging limit"
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return "provider returned a non-JSON error body"
    if not isinstance(payload, dict):
        return "provider returned an unsupported error response"
    error = payload.get("error", payload)
    if not isinstance(error, dict):
        return "provider returned an unsupported error response"
    detail = {
        field: str(error[field])
        for field in ("message", "type", "code")
        if isinstance(error.get(field), (str, int, float))
    }
    if not detail:
        return "provider returned no structured error details"
    message = json.dumps(detail, ensure_ascii=True)[:1000]
    return _redact_provider_error(message)


def _redact_provider_error(message: str) -> str:
    message = re.sub(r"(?i)(bearer\s+)[^\s,\"']+", r"\1[REDACTED]", message)
    message = re.sub(r"(?i)gsk_[a-z0-9_-]+", "[REDACTED]", message)
    for name in (
        "GROQ_API_KEY",
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "UPCDATABASE_API_KEY",
        "UPCITEMDB_USER_KEY",
        "EBAY_CLIENT_SECRET",
    ):
        secret = os.environ.get(name, "").strip()
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message


def _http_error_log_detail(exc: urllib.error.HTTPError) -> str:
    try:
        raw_body = exc.read(_MAX_PROVIDER_ERROR_BYTES + 1)
    except OSError:
        return "error response body could not be read"
    return _provider_error_log_detail(raw_body)


def _provider_http_error_message(status_code: int) -> str:
    return {
        401: "Provider credentials were rejected.",
        403: "Provider access is forbidden; check the account plan and API permissions.",
        404: "No matching product was found.",
        429: "Provider rate limit reached. Try again later.",
    }.get(status_code, f"Provider request failed with HTTP {status_code}.")


def _tesseract_path() -> str | None:
    configured = os.environ.get("TESSERACT_CMD", "").strip()
    if configured:
        return configured if os.path.isfile(configured) else None
    return shutil.which("tesseract")


def provider_statuses() -> list[dict[str, str | bool]]:
    try:
        import zxingcpp

        zxing_available = callable(getattr(zxingcpp, "read_barcodes", None))
    except ImportError:
        zxing_available = False
    tesseract = _tesseract_path()
    configured = {
        "vision_gemini": bool(os.environ.get("GEMINI_API_KEY", "").strip()),
        "vision_openai": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
        "vision_groqcloud": bool(os.environ.get("GROQ_API_KEY", "").strip()),
        "barcode_local": zxing_available and bool(tesseract),
        "upcitemdb": True,
        "upcdatabase": bool(os.environ.get("UPCDATABASE_API_KEY", "").strip()),
        "ebay_active": bool(
            os.environ.get("EBAY_CLIENT_ID", "").strip()
            and os.environ.get("EBAY_CLIENT_SECRET", "").strip()
        ),
    }
    detail = {
        "vision_gemini": (
            os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
            if configured["vision_gemini"]
            else "Set GEMINI_API_KEY in .env"
        ),
        "vision_openai": (
            os.environ.get("OPENAI_VISION_MODEL", "gpt-4o-mini")
            if configured["vision_openai"]
            else "Set OPENAI_API_KEY in .env"
        ),
        "vision_groqcloud": (
            os.environ.get("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")
            if configured["vision_groqcloud"]
            else "Set GROQ_API_KEY in .env"
        ),
        "barcode_local": (
            "ZXing barcode reader and local Tesseract OCR"
            if zxing_available and tesseract
            else "Install zxing-cpp and Tesseract OCR to enable local barcode/OCR"
        ),
        "upcitemdb": "UPC lookups use the public trial endpoint unless a paid API key is configured",
        "upcdatabase": (
            "UPCDATABASE_API_KEY is configured"
            if configured["upcdatabase"]
            else "Set UPCDATABASE_API_KEY in .env"
        ),
        "ebay_active": (
            "eBay Browse API credentials are configured"
            if configured["ebay_active"]
            else "Set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET in .env"
        ),
    }
    providers = [
        ("vision_gemini", "Gemini vision", "Image identification"),
        ("vision_openai", "OpenAI vision", "Optional image comparison"),
        ("vision_groqcloud", "GroqCloud vision", "Optional image comparison (up to 3 photos)"),
        ("barcode_local", "Local barcode and OCR", "Runs only on this server"),
        ("upcitemdb", "UPCitemdb", "Product lookup by UPC/EAN"),
        ("upcdatabase", "UPCDatabase.org", "Product lookup by UPC/EAN"),
        ("ebay_active", "eBay active listings", "Current asking prices, not sold prices"),
    ]
    return [
        {
            "id": provider_id,
            "name": name,
            "group": group,
            "configured": configured[provider_id],
            "detail": detail[provider_id],
        }
        for provider_id, name, group in providers
    ]


def _request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    encoded = json.dumps(body).encode("utf-8") if body is not None else None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if encoded is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=encoded, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=_API_TIMEOUT_SECONDS) as response:
            payload = response.read(2 * 1024 * 1024 + 1)
            if len(payload) > 2 * 1024 * 1024:
                raise ProviderError("Provider returned an unexpectedly large response.")
            try:
                parsed = json.loads(payload)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ProviderError("Provider returned an invalid JSON response.") from exc
            if not isinstance(parsed, dict):
                raise ProviderError("Provider returned an unsupported response format.")
            return parsed, dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        message = _provider_http_error_message(exc.code)
        logger.warning(
            "Provider request failed: host=%s status=%d detail=%s",
            urllib.parse.urlsplit(url).hostname or "unknown",
            exc.code,
            _http_error_log_detail(exc),
        )
        raise ProviderError(message) from exc
    except urllib.error.URLError as exc:
        raise ProviderError("Provider could not be reached. Check the connection and try again.") from exc
    except TimeoutError as exc:
        raise ProviderError("Provider request timed out. The intake was kept; try again later.") from exc


def _candidate(
    source_name: str,
    source_type: str,
    fields: dict[str, str],
    evidence: list[dict[str, str]] | None = None,
    *,
    match_notes: str = "",
) -> dict[str, Any]:
    field_limits = {
        "name": 200,
        "brand": 120,
        "model": 120,
        "manufacturer_part_number": 120,
        "barcode": 80,
        "category": 80,
        "description": 3000,
    }
    result: dict[str, Any] = {
        "source_name": source_name,
        "source_type": source_type,
        "fields": {
            key: value.strip()[:field_limits[key]]
            for key, value in fields.items()
            if key in field_limits and isinstance(value, str) and value.strip()
        },
        "price_evidence": evidence or [],
    }
    if match_notes:
        result["match_notes"] = match_notes
    return result


def _currency_amount(value: Any) -> str | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0 or amount > Decimal("10000000"):
        return None
    return f"{amount:.2f}"


def _evidence(
    evidence_type: str,
    title: str,
    source_name: str,
    amount: Any,
    currency: str = "USD",
    product_url: str = "",
) -> dict[str, str] | None:
    parsed_amount = _currency_amount(amount)
    if parsed_amount is None:
        return None
    safe_url = product_url[:2000] if product_url.startswith(("https://", "http://")) else ""
    normalized_currency = currency[:3].upper() if currency else ""
    if not re.fullmatch(r"[A-Z]{3}", normalized_currency):
        return None
    return {
        "id": str(uuid.uuid4()),
        "evidence_type": evidence_type,
        "title": title[:240],
        "source_name": source_name,
        "product_url": safe_url,
        "amount": parsed_amount,
        "currency": normalized_currency,
    }


def _identifier(barcode: str, manufacturer_part_number: str) -> str:
    return barcode.strip() or manufacturer_part_number.strip()


def _normalize_upcitemdb(data: dict[str, Any], identifier: str) -> list[dict[str, Any]]:
    if data.get("code") not in (None, "OK", "SUCCESS"):
        raise ProviderError(f"UPCitemdb returned status {str(data.get('code'))[:80]}.")
    products = data.get("items")
    if not isinstance(products, list) or not products:
        raise ProviderError("UPCitemdb found no product for this barcode.")
    candidates: list[dict[str, Any]] = []
    for product in products[:5]:
        if not isinstance(product, dict):
            continue
        fields = {
            "name": str(product.get("title") or ""),
            "brand": str(product.get("brand") or ""),
            "model": str(product.get("model") or ""),
            "manufacturer_part_number": str(product.get("mpn") or ""),
            "barcode": str(product.get("upc") or product.get("ean") or identifier),
            "category": str(product.get("category") or ""),
            "description": str(product.get("description") or ""),
        }
        evidence: list[dict[str, str]] = []
        offers = product.get("offers")
        if isinstance(offers, list):
            for offer in offers[:5]:
                if not isinstance(offer, dict):
                    continue
                price = _evidence(
                    "retail_reference",
                    str(offer.get("title") or "UPCitemdb offer"),
                    "UPCitemdb offer",
                    offer.get("price"),
                    str(offer.get("currency") or "USD"),
                    str(offer.get("link") or offer.get("url") or ""),
                )
                if price:
                    evidence.append(price)
        candidate = _candidate("UPCitemdb", "upcitemdb", fields, evidence)
        if candidate["fields"]:
            candidates.append(candidate)
    if not candidates:
        raise ProviderError("UPCitemdb returned no usable product fields.")
    return candidates


def _upcitemdb(barcode: str) -> list[dict[str, Any]]:
    if not barcode.strip().isdigit():
        raise ProviderError("UPCitemdb requires a numeric UPC/EAN barcode; MPN-only lookup is not supported.")
    key = os.environ.get("UPCITEMDB_USER_KEY", "").strip()
    endpoint = "https://api.upcitemdb.com/prod/"
    headers = {}
    if key:
        endpoint += "v1/"
        headers = {
            "user_key": key,
            "key_type": os.environ.get("UPCITEMDB_KEY_TYPE", "3scale").strip(),
        }
    else:
        endpoint += "trial/"
    url = endpoint + "lookup?" + urllib.parse.urlencode({"upc": barcode})
    data, response_headers = _request_json(url, headers=headers)
    candidates = _normalize_upcitemdb(data, barcode)
    if response_headers.get("X-RateLimit-Remaining") == "0":
        candidates[0]["match_notes"] = "UPCitemdb trial quota is exhausted for the current period."
    return candidates


def _upcdatabase(barcode: str) -> list[dict[str, Any]]:
    if not barcode.strip().isdigit():
        raise ProviderError("UPCDatabase.org requires a numeric UPC/EAN barcode; MPN-only lookup is not supported.")
    key = os.environ.get("UPCDATABASE_API_KEY", "").strip()
    if not key:
        raise ProviderError("Set UPCDATABASE_API_KEY in .env before using UPCDatabase.org.")
    url = "https://api.upcdatabase.org/product/" + urllib.parse.quote(barcode, safe="")
    data, _ = _request_json(url, headers={"Authorization": key})
    if data.get("success") is False or data.get("error"):
        raise ProviderError("UPCDatabase.org did not return a product for this barcode.")
    stores = data.get("stores")
    evidence: list[dict[str, str]] = []
    if isinstance(stores, list):
        for store in stores[:5]:
            if not isinstance(store, dict):
                continue
            price = _evidence(
                "retail_reference",
                str(store.get("store") or "Store price"),
                "UPCDatabase.org store listing",
                store.get("price"),
                str(store.get("currency") or "USD"),
                str(store.get("url") or ""),
            )
            if price:
                evidence.append(price)
    msrp = _evidence(
        "retail_reference",
        "Manufacturer suggested retail price",
        "UPCDatabase.org",
        data.get("msrp"),
    )
    if msrp:
        evidence.append(msrp)
    candidate = _candidate(
        "UPCDatabase.org",
        "upcdatabase",
        {
            "name": str(data.get("title") or data.get("name") or ""),
            "brand": str(data.get("brand") or data.get("manufacturer") or ""),
            "manufacturer_part_number": str(data.get("mpn") or ""),
            "barcode": str(data.get("barcode") or data.get("upc") or barcode),
            "category": str(data.get("category") or ""),
            "description": str(data.get("description") or ""),
        },
        evidence,
    )
    if not candidate["fields"]:
        raise ProviderError("UPCDatabase.org returned no usable product fields.")
    return [candidate]


def _prepare_vision_image(data: bytes) -> tuple[str, str]:
    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    except (OSError, ValueError) as exc:
        raise ProviderError("Could not prepare a selected photo for the vision provider.") from exc
    image.thumbnail((1600, 1600))
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=82, optimize=True)
    return "image/jpeg", base64.b64encode(output.getvalue()).decode("ascii")


_VISION_PROMPT = (
    "Identify the retail product in this photo for an inventory intake. Return only one JSON object "
    "with string fields name, brand, model, manufacturer_part_number, barcode, category, description, "
    "and match_notes. Leave uncertain fields empty. Do not infer an exact UPC/MPN unless legible. "
    "Keep description factual and concise. This is an unverified suggestion for a human reviewer."
)


def _extract_json_text(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ProviderError("Vision provider did not return the requested structured product data.") from exc
    if not isinstance(value, dict):
        raise ProviderError("Vision provider returned an unsupported result shape.")
    return value


def _vision(method: str, photos: list[ProviderPhoto]) -> list[dict[str, Any]]:
    if not photos:
        raise ProviderError("Select at least one photo to send to the vision provider.")
    if method == "vision_groqcloud" and len(photos) > 3:
        raise ProviderError("GroqCloud vision accepts no more than 3 photos per request.")
    if len(photos) > _MAX_VISION_PHOTOS:
        raise ProviderError(f"Select no more than {_MAX_VISION_PHOTOS} photos for one vision request.")
    if sum(len(photo[1]) for photo in photos) > _MAX_VISION_BYTES:
        raise ProviderError("Selected photos exceed the total vision upload limit of 15 MB.")
    parts = [_prepare_vision_image(data) for _, data, _ in photos]
    if sum(len(data) * 3 // 4 for _, data in parts) > _MAX_VISION_ENCODED_BYTES:
        raise ProviderError("Resized photos exceed the maximum image payload; select fewer or smaller photos.")
    if method == "vision_gemini":
        key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            raise ProviderError("Set GEMINI_API_KEY in .env before using Gemini vision.")
        model = urllib.parse.quote(os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip(), safe="-.")
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        request_parts: list[dict[str, Any]] = [
            {"inlineData": {"mimeType": mime, "data": data}} for mime, data in parts
        ]
        request_parts.append({"text": _VISION_PROMPT})
        response, _ = _request_json(
            url,
            method="POST",
            headers={"X-Goog-Api-Key": key},
            body={"contents": [{"parts": request_parts}], "generationConfig": {"responseMimeType": "application/json"}},
        )
        try:
            text = response["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("Gemini returned no product-identification candidate.") from exc
        source, source_type = "Google Gemini", "vision_gemini"
    elif method == "vision_openai":
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise ProviderError("Set OPENAI_API_KEY in .env before using OpenAI vision.")
        model = os.environ.get("OPENAI_VISION_MODEL", "gpt-4o-mini").strip()
        content: list[dict[str, Any]] = [{"type": "input_text", "text": _VISION_PROMPT}]
        for mime, data in parts:
            content.append({"type": "input_image", "image_url": f"data:{mime};base64,{data}"})
        response, _ = _request_json(
            "https://api.openai.com/v1/responses",
            method="POST",
            headers={"Authorization": f"Bearer {key}"},
            body={"model": model, "input": [{"role": "user", "content": content}]},
        )
        text = response.get("output_text")
        if not isinstance(text, str):
            output = response.get("output", [])
            text = next(
                (
                    part.get("text")
                    for item in output if isinstance(item, dict)
                    for part in item.get("content", []) if isinstance(part, dict) and isinstance(part.get("text"), str)
                ),
                "",
            )
        if not text:
            raise ProviderError("OpenAI returned no product-identification candidate.")
        source, source_type = "OpenAI vision", "vision_openai"
    elif method == "vision_groqcloud":
        key = os.environ.get("GROQ_API_KEY", "").strip()
        if not key:
            raise ProviderError("Set GROQ_API_KEY in .env before using GroqCloud vision.")
        try:
            from groq import APIConnectionError, APIStatusError, APITimeoutError, Groq
            from groq.types.chat import (
                ChatCompletionContentPartImageParam,
                ChatCompletionContentPartParam,
                ChatCompletionContentPartTextParam,
                ChatCompletionUserMessageParam,
            )
        except ImportError as exc:
            raise ProviderError("Install the groq package to use GroqCloud vision.") from exc
        model = os.environ.get("GROQ_VISION_MODEL", "qwen/qwen3.8-27b").strip()
        groq_content: list[ChatCompletionContentPartParam] = []
        text_part: ChatCompletionContentPartTextParam = {"type": "text", "text": _VISION_PROMPT}
        groq_content.append(text_part)
        for mime, data in parts:
            image_part: ChatCompletionContentPartImageParam = {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{data}"},
            }
            groq_content.append(image_part)
        message: ChatCompletionUserMessageParam = {"role": "user", "content": groq_content}
        try:
            with Groq(api_key=key, timeout=_API_TIMEOUT_SECONDS, max_retries=0) as client:
                response = client.chat.completions.create(
                    model=model,
                    messages=[message],
                    response_format={"type": "json_object"},
                )
        except APIStatusError as exc:
            logger.warning(
                "GroqCloud request failed: host=api.groq.com status=%d detail=%s",
                exc.status_code,
                _provider_error_log_detail(exc.body),
            )
            raise ProviderError(_provider_http_error_message(exc.status_code)) from exc
        except APITimeoutError as exc:
            raise ProviderError("GroqCloud request timed out. The intake was kept; try again later.") from exc
        except APIConnectionError as exc:
            raise ProviderError("GroqCloud could not be reached. Check the connection and try again.") from exc
        try:
            text = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise ProviderError("GroqCloud returned no product-identification candidate.") from exc
        if not isinstance(text, str) or not text:
            raise ProviderError("GroqCloud returned no product-identification candidate.")
        source, source_type = "GroqCloud vision", "vision_groqcloud"
    else:
        raise ProviderError("Unknown vision provider method.")
    value = _extract_json_text(text)
    fields = {
        field: str(value.get(field) or "")
        for field in (
            "name", "brand", "model", "manufacturer_part_number", "barcode", "category", "description"
        )
    }
    notes = str(value.get("match_notes") or "")[:500]
    candidate = _candidate(source, source_type, fields, match_notes=notes)
    if not candidate["fields"]:
        raise ProviderError("Vision provider returned no usable product fields.")
    return [candidate]


def _local_barcode_ocr(photos: list[ProviderPhoto]) -> list[dict[str, Any]]:
    if not photos:
        raise ProviderError("Select at least one barcode or package photo for local scanning.")
    try:
        import zxingcpp
    except ImportError as exc:
        raise ProviderError("Install zxing-cpp to enable local barcode reading.") from exc
    try:
        import pytesseract
    except ImportError as exc:
        raise ProviderError("Install pytesseract to enable local OCR.") from exc
    tesseract = _tesseract_path()
    if not tesseract:
        raise ProviderError("Install Tesseract OCR and set TESSERACT_CMD if it is not on PATH.")
    pytesseract.pytesseract.tesseract_cmd = tesseract

    barcodes: list[str] = []
    lines: list[str] = []
    for _, data, _ in photos:
        try:
            image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
            barcodes.extend(result.text for result in zxingcpp.read_barcodes(image) if result.text)
            text = pytesseract.image_to_string(image, timeout=8)
        except RuntimeError as exc:
            raise ProviderError("Local OCR timed out or failed. The staged intake remains available.") from exc
        except (OSError, ValueError) as exc:
            raise ProviderError("Could not read one of the selected photos locally.") from exc
        lines.extend(line.strip() for line in text.splitlines() if line.strip())

    barcode = next((value for value in barcodes if value.isdigit() and len(value) in {8, 12, 13, 14}), "")
    mpn = next(
        (
            line for line in lines
            if 4 <= len(line) <= 40
            and re.search(r"[A-Za-z]", line)
            and re.search(r"\d", line)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ./_-]*", line)
        ),
        "",
    )
    fields = {"barcode": barcode, "manufacturer_part_number": mpn}
    notes = "OCR text is a suggestion and may be noisy; verify both identifiers manually."
    if not barcode and not mpn:
        notes = "No barcode or likely manufacturer part number was detected. Check the photo and enter identifiers manually."
    candidate = _candidate("Local ZXing + Tesseract", "barcode_local", fields, match_notes=notes)
    return [candidate]


def _get_ebay_token() -> str:
    global _ebay_token
    now = time.monotonic()
    with _ebay_token_lock:
        if _ebay_token and _ebay_token[1] > now + 30:
            return _ebay_token[0]
        client_id = os.environ.get("EBAY_CLIENT_ID", "").strip()
        client_secret = os.environ.get("EBAY_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise ProviderError("Set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET in .env before using eBay.")
        auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"}).encode()
        request = urllib.request.Request(
            "https://api.ebay.com/identity/v1/oauth2/token",
            data=body,
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=_API_TIMEOUT_SECONDS) as response:
                token_data = json.loads(response.read(64 * 1024))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            raise ProviderError("eBay OAuth authentication failed; verify credentials and API access.") from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProviderError("eBay OAuth returned an invalid response.") from exc
        token = token_data.get("access_token")
        expires_in = token_data.get("expires_in")
        if not isinstance(token, str) or not isinstance(expires_in, int):
            raise ProviderError("eBay OAuth returned no usable access token.")
        _ebay_token = (token, now + expires_in)
        return token


def _ebay_active(barcode: str, manufacturer_part_number: str) -> list[dict[str, Any]]:
    identifier = _identifier(barcode, manufacturer_part_number)
    if not identifier:
        raise ProviderError("Enter a barcode or manufacturer part number before searching eBay.")
    params = {"gtin": identifier} if barcode else {"q": manufacturer_part_number}
    url = "https://api.ebay.com/buy/browse/v1/item_summary/search?" + urllib.parse.urlencode(params)
    data, _ = _request_json(
        url,
        headers={
            "Authorization": "Bearer " + _get_ebay_token(),
            "X-EBAY-C-MARKETPLACE-ID": os.environ.get("EBAY_MARKETPLACE_ID", "EBAY_US").strip(),
        },
    )
    summaries = data.get("itemSummaries")
    if not isinstance(summaries, list) or not summaries:
        raise ProviderError("eBay Browse found no active listings for this identifier.")
    candidates: list[dict[str, Any]] = []
    for summary in summaries[:8]:
        if not isinstance(summary, dict):
            continue
        price_data = summary.get("price")
        price = None
        if isinstance(price_data, dict):
            price = _evidence(
                "active_listing",
                str(summary.get("title") or "Active eBay listing"),
                "eBay Browse API",
                price_data.get("value"),
                str(price_data.get("currency") or "USD"),
                str(summary.get("itemWebUrl") or ""),
            )
        fields = {"name": str(summary.get("title") or "")}
        evidence = [price] if price else []
        candidate = _candidate("eBay Browse API (active listings)", "ebay_active", fields, evidence)
        if candidate["fields"] or evidence:
            candidates.append(candidate)
    if not candidates:
        raise ProviderError("eBay returned no usable active listing details.")
    return candidates


def run_provider(
    method: str,
    *,
    barcode: str,
    manufacturer_part_number: str,
    photos: list[ProviderPhoto],
) -> list[dict[str, Any]]:
    if method in {"vision_gemini", "vision_openai", "vision_groqcloud"}:
        return _vision(method, photos)
    if method == "barcode_local":
        return _local_barcode_ocr(photos)
    if method == "upcitemdb":
        if not barcode:
            raise ProviderError("UPCitemdb lookup requires a barcode; MPN-only search is not supported.")
        return _upcitemdb(barcode)
    if method == "upcdatabase":
        if not barcode:
            raise ProviderError("UPCDatabase.org lookup requires a barcode; MPN-only search is not supported.")
        return _upcdatabase(barcode)
    if method == "ebay_active":
        return _ebay_active(barcode, manufacturer_part_number)
    raise ProviderError("Unknown provider method.")

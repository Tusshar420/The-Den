"""
The one place the assistant talks HTTP (stdlib urllib, like providers.py).

Every provider call goes through request(), so error text is scrubbed of
anything that looks like a credential before it can reach a log line, a task
result or the dashboard - and tests can swap this single function for a fake.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

# Access tokens (ya29.), refresh tokens (1//), bearer headers, JWTs, and
# token-looking key=value / "key": "value" pairs.
_SECRET_RE = re.compile(
    r"(ya29\.[\w.\-]+|1//[\w.\-]+|Bearer\s+[\w.~+/=\-]+|eyJ[\w\-]+\.[\w\-]+\.[\w\-]+"
    r"|(?:access_token|refresh_token|id_token|client_secret|code_verifier)[\"']?\s*[:=]\s*[\"']?[\w.~+/=\-]+)",
    re.IGNORECASE)


def scrub(text: Any) -> str:
    return _SECRET_RE.sub("[redacted]", str(text))


class ApiError(Exception):
    def __init__(self, message: str, status: int = 0, retry_after: Optional[int] = None, code: str = ""):
        super().__init__(scrub(message))
        self.status = status
        self.retry_after = retry_after
        # OAuth / Google error reason, e.g. "invalid_grant", "notFound"
        self.code = code

    @property
    def retryable(self) -> bool:
        return self.status in (429, 500, 502, 503, 504)


def request(method: str, url: str, *, headers: Optional[dict[str, str]] = None,
            params: Optional[dict[str, Any]] = None, json_body: Optional[Any] = None,
            form: Optional[dict[str, str]] = None, timeout: int = 20,
            raw_body: Optional[bytes] = None, content_type: Optional[str] = None) -> Any:
    """JSON in, JSON out ({} for an empty body). Raises ApiError."""
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(clean, doseq=True)
    hdrs = dict(headers or {})
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    elif raw_body is not None:
        data = raw_body
        hdrs["Content-Type"] = content_type or "application/octet-stream"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raw_err = e.read().decode("utf-8", errors="replace")
        message, code = raw_err[:300] or str(e), ""
        try:
            body = json.loads(raw_err)
            err = body.get("error")
            if isinstance(err, dict):  # Google APIs
                message = err.get("message") or message
                errors = err.get("errors") or [{}]
                code = errors[0].get("reason") or err.get("status") or ""
            elif isinstance(err, str):  # OAuth token endpoints
                code = err
                message = body.get("error_description") or err
        except (ValueError, AttributeError):
            pass
        retry = e.headers.get("Retry-After") if e.headers else None
        raise ApiError(f"{e.code}: {message}", status=e.code, code=str(code),
                       retry_after=int(float(retry)) if retry and retry.replace(".", "", 1).isdigit() else None)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ApiError(f"Could not reach {urllib.parse.urlparse(url).netloc}: {e}")
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ApiError(f"Unexpected non-JSON reply from {urllib.parse.urlparse(url).netloc}")

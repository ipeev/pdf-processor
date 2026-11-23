#!/usr/bin/env python3
"""Zero-dependency frontend/proxy smoke test (runs inside the API container)."""
import argparse
import json
import re
import sys
from urllib.error import URLError, HTTPError
from urllib.request import urlopen


def fetch(url: str) -> tuple[int, str, bytes]:
    with urlopen(url, timeout=8) as response:
        return response.status, response.headers.get("Content-Type", ""), response.read(1024 * 1024)


def check(base_url: str) -> None:
    base_url = base_url.rstrip("/")
    status, content_type, body = fetch(base_url + "/")
    html = body.decode("utf-8", errors="replace")
    if status != 200 or "text/html" not in content_type or 'id="root"' not in html or "PDF Processor" not in html:
        raise AssertionError("Frontend HTML is missing or invalid")
    js = re.search(r'src="(/assets/[^" ]+\.js)"', html)
    if not js:
        raise AssertionError("Vite build JS asset not referenced in frontend HTML")
    js_status, js_content_type, js_body = fetch(base_url + js.group(1))
    if js_status != 200 or len(js_body) < 100 or "javascript" not in js_content_type:
        raise AssertionError("Built React JavaScript bundle is missing")
    if b'/api/v1/system/status' not in js_body:
        raise AssertionError("Vite JavaScript bundle is missing the System Monitor view")
    css = re.search(r'href="(/assets/[^" ]+\.css)"', html)
    if not css:
        raise AssertionError("Vite build CSS asset not referenced in frontend HTML")
    css_status, css_content_type, css_body = fetch(base_url + css.group(1))
    if (css_status != 200 or "text/css" not in css_content_type or
            not all(marker in css_body for marker in (b".app-shell", b".dropzone", b".results-panel"))):
        raise AssertionError("Redesigned React CSS bundle is missing")
    for route in ("/system", "/upload", "/library", "/runs/12345678-1234-1234-1234-123456789abc", "/library/" + "a" * 64):
        route_status, route_type, route_body = fetch(base_url + route)
        if route_status != 200 or "text/html" not in route_type or 'id="root"' not in route_body.decode("utf-8", errors="replace"):
            raise AssertionError(f"React {route} navigation fallback is unavailable")
    for marker in (b"Processing History", b"Document Library", b"Permanent archive", b"/api/v1/runs/", b"/api/v1/library/", b"Currently processing", b"No active processing jobs"):
        if marker not in js_body:
            raise AssertionError(f"Built frontend missing persistent workspace marker: {marker!r}")
    status, _, body = fetch(base_url + "/api/v1/ready")
    try:
        ready = json.loads(body)
    except (ValueError, TypeError):
        ready = None
    if status != 200 or not isinstance(ready, dict) or ready.get('redis') != 'ok':
        raise AssertionError("Nginx /api/v1 proxy is not reaching FastAPI + Redis")
    system_status, _, system_body = fetch(base_url + "/api/v1/system/status")
    try:
        monitor = json.loads(system_body)
    except (ValueError, TypeError):
        monitor = None
    if (system_status != 200 or not isinstance(monitor, dict) or
            not isinstance(monitor.get("redis"), dict) or
            not isinstance(monitor.get("rustfs"), dict) or
            not isinstance(monitor.get("openrouter"), dict) or
            not isinstance(monitor.get("opensearch"), dict) or
            monitor["opensearch"].get("status") != "online" or
            not isinstance(monitor.get("archive"), dict) or
            "consistency" not in monitor["archive"] or
            "recovery" not in monitor["archive"]):
        raise AssertionError("Live system-monitor API is unavailable through Nginx")
    for route in ("library", "runs"):
        code, kind, payload = fetch(base_url + f"/api/v1/{route}?limit=1")
        try:
            page = json.loads(payload)
        except (ValueError, TypeError):
            page = None
        if (code != 200 or "application/json" not in kind or not isinstance(page, dict)
                or not isinstance(page.get("items"), list) or len(page["items"]) > 1
                or "next_cursor" not in page or (page["next_cursor"] is not None and not isinstance(page["next_cursor"], str))):
            raise AssertionError(f"Persistent {route} API is unavailable through Nginx")
    print("PASS React History, Library, Upload, run/document deep links, System Monitor and same-origin API proxy")
    print("PASS permanent library and processing history lists through same-origin Nginx proxy")
    print("PASS permanent archive monitor contract through same-origin proxy")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://frontend")
    args = parser.parse_args()
    try:
        check(args.base_url)
        return 0
    except (AssertionError, URLError, HTTPError, OSError) as exc:
        print(f"FAIL frontend smoke test: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Zero-dependency HTTP end-to-end tester for the PDF processor.

Does not require an application import or a .env file. Standard-library only.
Uploads, remembers every returned document ID, polls each until terminal,
checks page content and summaries, and reports actionable failures.

IMPORTANT: quick, full and custom modes spend OpenRouter credits via the worker.
The check mode performs only health/readiness requests.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class CheckFailure(Exception):
    """Readable verification failure (with no secrets or full documents)."""


@dataclass(frozen=True)
class DocumentExpectation:
    filename: str
    page_count: int | None = None
    page_text: dict[int, str] = field(default_factory=dict)
    blank_pages: tuple[int, ...] = ()
    summary_text: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class Scenario:
    name: str
    parser: str
    files: tuple[Path, ...]
    expected: tuple[DocumentExpectation, ...]


@dataclass
class CheckResult:
    scenario: str
    document_id: str
    filename: str
    parser: str
    status: str
    page_count: int | None
    error_code: str | None
    elapsed_seconds: float


def api_base(raw: str) -> str:
    """Accept either http://host:port or http://host:port/api/v1."""
    address = raw.rstrip("/")
    if not address.startswith(("http://", "https://")):
        raise CheckFailure("--base-url must begin with http:// or https://")
    if address.endswith("/api/v1"):
        return address
    if "/api/" in address:
        raise CheckFailure("Use a base URL ending in /api/v1, not a specific API endpoint")
    return address + "/api/v1"


def multipart(files: tuple[Path, ...], parser: str) -> tuple[bytes, str]:
    boundary = "----pdfcheck-" + uuid.uuid4().hex
    chunks: list[bytes] = []
    chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"parser\"\r\n\r\n{parser}\r\n".encode())
    for path in files:
        if not path.is_file():
            raise CheckFailure(f"PDF not found: {path}")
        if path.stat().st_size > 20 * 1024 * 1024:
            raise CheckFailure(f"Sample exceeds 20 MiB demo upload limit: {path}")
        filename = path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; "
            f"filename=\"{filename}\"\r\nContent-Type: application/pdf\r\n\r\n".encode()
        )
        chunks.append(path.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class WorkflowClient:
    def __init__(self, base_url: str, request_timeout: float = 25.0):
        self.base_url = api_base(base_url)
        self.request_timeout = request_timeout

    def request(self, method: str, path: str, *, body: bytes | None = None,
                content_type: str | None = None) -> tuple[int, dict[str, Any]]:
        headers = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        req = Request(self.base_url + path, data=body, method=method, headers=headers)
        try:
            with urlopen(req, timeout=self.request_timeout) as res:
                code, raw = res.status, res.read(4 * 1024 * 1024 + 1)
        except HTTPError as err:
            code, raw = err.code, err.read(4 * 1024 * 1024 + 1)
        except (URLError, TimeoutError, OSError) as exc:
            raise CheckFailure(
                f"Cannot reach {self.base_url}: {exc}. "
                "Check 'docker compose ps' and 'docker compose logs --tail=50 api worker'."
            ) from exc
        if len(raw) > 4 * 1024 * 1024:
            raise CheckFailure(f"HTTP {code} {path}: response exceeds 4 MiB inspection limit")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise CheckFailure(f"HTTP {code} {path}: response is not valid JSON") from exc
        if not isinstance(data, dict):
            raise CheckFailure(f"HTTP {code} {path}: response must be a JSON object")
        return code, data

    def check_ready(self) -> None:
        for path, expected in (("/health", "ok"), ("/ready", "ready")):
            status, body = self.request("GET", path)
            if status != 200 or body.get("status") != expected:
                err = body.get("error")
                detail = err.get("code") if isinstance(err, dict) else body.get("status")
                raise CheckFailure(f"{path}: expected HTTP 200 / {expected}, got HTTP {status} ({detail})")
        print("PASS API health and Redis readiness", flush=True)

    def upload(self, scenario: Scenario) -> list[str]:
        encoded, content_type = multipart(scenario.files, scenario.parser)
        status, body = self.request("POST", "/documents", body=encoded, content_type=content_type)
        if status != 202:
            err = body.get("error")
            detail = err.get("code") if isinstance(err, dict) else str(body)[:160]
            raise CheckFailure(f"Upload {scenario.name}: expected HTTP 202, got {status} ({detail}). "
                               "Do not blindly retry an uncertain upload commit.")
        docs = body.get("documents")
        if not isinstance(docs, list) or len(docs) != len(scenario.files):
            raise CheckFailure(f"Upload {scenario.name}: expected {len(scenario.files)} accepted documents")
        ids: list[str] = []
        for index, doc in enumerate(docs):
            if not isinstance(doc, dict):
                raise CheckFailure(f"Upload {scenario.name}: malformed accepted document #{index + 1}")
            try:
                identifier = str(uuid.UUID(doc["id"]))
            except (KeyError, ValueError, AttributeError, TypeError) as exc:
                raise CheckFailure(f"Upload {scenario.name}: missing/invalid document UUID") from exc
            if doc.get("filename") != scenario.files[index].name or doc.get("parser") != scenario.parser:
                raise CheckFailure(f"Upload {scenario.name}: wrong filename or parser on item #{index + 1}")
            if doc.get("status") not in {"queued", "completed"}:
                raise CheckFailure(f"Upload {scenario.name}: unexpected status in HTTP 202")
            if doc.get("status") == "completed" and doc.get("cache") != "hit":
                raise CheckFailure(f"Upload {scenario.name}: immediate completion requires a cache hit")
            ids.append(identifier)
        if len(set(ids)) != len(ids):
            raise CheckFailure(f"Upload {scenario.name}: duplicate document IDs")
        # The pages endpoint returns 409 until extraction is stored.
        # The worker may race ahead, so HTTP 200 is also valid here.
        for identifier in ids:
            early_status, early_body = self.request("GET", f"/documents/{identifier}/pages")
            if early_status == 409:
                err = early_body.get("error")
                if not isinstance(err, dict) or err.get("code") != "RESULT_NOT_READY":
                    raise CheckFailure(f"Upload {scenario.name}: pages returned wrong 409 error for {identifier}")
            elif early_status != 200:
                raise CheckFailure(f"Upload {scenario.name}: premature pages returned HTTP {early_status}")
        print(f"PASS HTTP 202 accepted {len(ids)} document(s) for {scenario.name}", flush=True)
        return ids

    def wait(self, identifier: str, scenario: Scenario, expected: DocumentExpectation,
             *, timeout: float, interval: float) -> CheckResult:
        began = time.monotonic()
        deadline = began + timeout
        last_state: str | None = None
        while True:
            status, doc = self.request("GET", f"/documents/{identifier}")
            if status != 200:
                raise CheckFailure(f"GET status {identifier}: expected HTTP 200, got {status}: {doc.get('error')}")
            if doc.get("id") != identifier or doc.get("filename") != expected.filename:
                raise CheckFailure(f"GET status {identifier}: document ID/filename mismatch")
            if doc.get("parser") != scenario.parser:
                raise CheckFailure(f"GET status {identifier}: parser mismatch")
            state = doc.get("status")
            if state not in ("queued", "processing", "summarizing", "partial", "completed", "failed"):
                raise CheckFailure(f"GET status {identifier}: unknown state {state!r}")
            if state != last_state:
                print(f"  {expected.filename}: {state} ({identifier})", flush=True)
                last_state = state
            if state in ("completed", "partial", "failed"):
                result = self.verify_terminal(identifier, scenario, expected, doc)
                result.elapsed_seconds = round(time.monotonic() - began, 3)
                return result
            if time.monotonic() >= deadline:
                raise CheckFailure(f"{expected.filename} ({identifier}): timed out after {timeout:.0f}s; "
                                   "inspect 'docker compose logs --tail=100 worker'")
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))

    def verify_terminal(self, identifier: str, scenario: Scenario,
                        expected: DocumentExpectation, doc: dict[str, Any]) -> CheckResult:
        state = doc["status"]
        err = doc.get("error")
        code = err.get("code") if isinstance(err, dict) else None
        if expected.error_code is not None:
            if state != "failed" or code != expected.error_code:
                raise CheckFailure(f"{expected.filename}: expected failed/{expected.error_code}, "
                                   f"got {state}/{code}")
            if doc.get("summary"):
                raise CheckFailure(f"{expected.filename}: failed document unexpectedly contains summary")
            pages_status, pages_body = self.request("GET", f"/documents/{identifier}/pages")
            pages_error = pages_body.get("error")
            if (pages_status != 409 or not isinstance(pages_error, dict)
                    or pages_error.get("code") != "RESULT_NOT_READY"):
                raise CheckFailure(f"{expected.filename}: failed job must not expose completed pages")
            print(f"PASS expected failure {expected.filename}: {code}", flush=True)
            return CheckResult(scenario.name, identifier, expected.filename, scenario.parser,
                               state, None, code, 0.0)
        if state != "completed":
            raise CheckFailure(f"{expected.filename}: job failed with {code or 'UNKNOWN'}: "
                               f"{err.get('message') if isinstance(err, dict) else ''}. "
                               "Inspect 'docker compose logs --tail=100 worker'.")
        summary = doc.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise CheckFailure(f"{expected.filename}: completed without a nonempty summary")
        if err is not None:
            raise CheckFailure(f"{expected.filename}: completed but error={err!r}")
        if expected.summary_text and expected.summary_text.lower() not in summary.lower():
            raise CheckFailure(f"{expected.filename}: summary is missing expected term {expected.summary_text!r}")
        status, body = self.request("GET", f"/documents/{identifier}/pages")
        if status != 200:
            raise CheckFailure(f"GET pages {identifier}: expected HTTP 200, got {status}")
        if body.get("id") != identifier:
            raise CheckFailure(f"GET pages {identifier}: document UUID mismatch")
        pages = body.get("pages")
        if not isinstance(pages, list) or not pages:
            raise CheckFailure(f"{expected.filename}: completed but pages missing/empty")
        indices = [page.get("page") if isinstance(page, dict) else None for page in pages]
        if indices != list(range(1, len(pages) + 1)):
            raise CheckFailure(f"{expected.filename}: invalid page numbering/order: {indices!r}")
        if doc.get("page_count") != len(pages):
            raise CheckFailure(f"{expected.filename}: status page_count differs from GET pages")
        if expected.page_count is not None and len(pages) != expected.page_count:
            raise CheckFailure(f"{expected.filename}: expected {expected.page_count} pages, got {len(pages)}")
        for page in pages:
            if not isinstance(page.get("markdown"), str):
                raise CheckFailure(f"{expected.filename}: page {page['page']} content is not a string")
        for page_number, needle in expected.page_text.items():
            if page_number > len(pages):
                raise CheckFailure(f"{expected.filename}: page {page_number} missing {needle!r}")
            content = pages[page_number - 1]["markdown"].lower()
            # Markdown often writes financial values with grouping separators:
            # 12,000 or 12 000 should both satisfy a 12000 fixture assertion.
            if needle.isdigit():
                content = re.sub(r"(?<=\d)[, _](?=\d)", "", content)
            if needle.lower() not in content:
                raise CheckFailure(f"{expected.filename}: page {page_number} missing {needle!r}")
        for page_number in expected.blank_pages:
            if page_number > len(pages) or pages[page_number - 1]["markdown"].strip():
                raise CheckFailure(f"{expected.filename}: page {page_number} should be blank")
        print(f"PASS completed {expected.filename}: {len(pages)} ordered pages + summary", flush=True)
        return CheckResult(scenario.name, identifier, expected.filename, scenario.parser,
                           state, len(pages), None, 0.0)


def sample_scenarios(mode: str, samples: Path) -> list[Scenario]:
    basic = samples / "basic_text.pdf"
    blank = samples / "blank_middle_page.pdf"
    table = samples / "table_and_headings.pdf"
    scan = samples / "scanned_image_only.pdf"
    starter = Scenario("basic-text", "pypdf", (basic,), (
        DocumentExpectation("basic_text.pdf", page_count=2,
                            page_text={1: "river flows to the sea", 2: "results are positive"},
                            summary_text="alpha"),
    ))
    if mode == "quick":
        return [starter]
    if mode != "full":
        raise CheckFailure(f"Unknown scenario mode: {mode}")
    return [
        Scenario("multi-upload-and-blank-page", "pypdf", (basic, blank), (
            starter.expected[0],
            DocumentExpectation("blank_middle_page.pdf", page_count=3,
                                page_text={1: "distinct sentence", 3: "closing remarks"}, blank_pages=(2,)),
        )),
        Scenario("gemini-markdown", "gemini", (table,), (
            DocumentExpectation("table_and_headings.pdf", page_count=2,
                                page_text={1: "12000", 2: "Summary and Findings"},
                                summary_text="revenue"),
        )),
        Scenario("gpt5-scanned-ocr", "gpt5_mini", (scan,), (
            DocumentExpectation("scanned_image_only.pdf", page_count=1,
                                page_text={1: "BLUE ORCHID"}, summary_text="BLUE ORCHID"),
        )),
        Scenario("scanned-pypdf-rejection", "pypdf", (scan,), (
            DocumentExpectation("scanned_image_only.pdf", error_code="NO_EXTRACTABLE_TEXT"),
        )),
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Automatically upload PDFs, track UUIDs, poll jobs and verify results. "
                    "quick/full/custom modes call paid OpenRouter models."
    )
    parser.add_argument("--mode", choices=("check", "quick", "full", "custom"), default="quick",
                        help="check=readiness only; quick=small PyPDF workflow; full=all parser scenarios; "
                             "custom=your files (default: quick)")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api/v1",
                        help="API base URL including optional /api/v1")
    parser.add_argument("--samples-dir", type=Path, default=Path(__file__).resolve().parents[1] / "samples")
    parser.add_argument("--file", type=Path, action="append", default=[], dest="files",
                        help="PDF to upload for custom mode; repeat for multi-upload")
    parser.add_argument("--parser", choices=("pypdf", "gemini", "gpt5_mini"), default="pypdf")
    parser.add_argument("--expect-text", help="Verify text occurs in at least one extracted page (custom)")
    parser.add_argument("--expect-summary-text", help="Verify text occurs in the summary (custom)")
    parser.add_argument("--expect-error", help="Verify terminal failure with error code instead of success (custom)")
    parser.add_argument("--timeout", type=float, default=180.0,
                        help="Maximum seconds to wait per document (default 180)")
    parser.add_argument("--poll", type=float, default=1.5, help="Seconds between polls (default 1.5)")
    parser.add_argument("--request-timeout", type=float, default=25.0)
    parser.add_argument("--show-results", action="store_true", help="Show final summaries (not default)")
    parser.add_argument("--report", type=Path, help="Write machine-readable JSON with status/IDs (no content)")
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.poll <= 0 or args.request_timeout <= 0:
        parser.error("--timeout, --poll, and --request-timeout must be positive")
    if args.mode != "custom" and (args.files or args.expect_error or args.expect_text or args.expect_summary_text):
        parser.error("--file and --expect-* require --mode custom")
    if args.mode == "custom" and not args.files:
        parser.error("--mode custom requires at least one --file")
    return args


def workflow(args: argparse.Namespace) -> list[CheckResult]:
    client = WorkflowClient(args.base_url, args.request_timeout)
    client.check_ready()
    if args.mode == "check":
        print("PASS check mode: no files uploaded, no OpenRouter charges", flush=True)
        return []
    print("NOTE: this mode uploads PDFs and invokes paid OpenRouter models through the worker.", flush=True)
    if args.mode == "custom":
        scenarios = [Scenario("custom", args.parser, tuple(args.files), tuple(
            DocumentExpectation(path.name, summary_text=args.expect_summary_text,
                                error_code=args.expect_error) for path in args.files
        ))]
    else:
        scenarios = sample_scenarios(args.mode, args.samples_dir)
    results: list[CheckResult] = []
    for scenario in scenarios:
        print(f"=== {scenario.name} [{scenario.parser}] ===", flush=True)
        ids = client.upload(scenario)
        for identifier, expected in zip(ids, scenario.expected, strict=True):
            result = client.wait(identifier, scenario, expected, timeout=args.timeout, interval=args.poll)
            if args.mode == "custom" and args.expect_text and result.status == "completed":
                _, body = client.request("GET", f"/documents/{identifier}/pages")
                if args.expect_text.lower() not in "\n".join(p["markdown"].lower() for p in body["pages"]):
                    raise CheckFailure(f"Custom PDF {expected.filename}: extracted pages missing {args.expect_text!r}")
            if args.show_results and result.status == "completed":
                _, body = client.request("GET", f"/documents/{identifier}")
                print(f"SUMMARY {expected.filename}: {body['summary']}", flush=True)
                _, page_body = client.request("GET", f"/documents/{identifier}/pages")
                for page in page_body["pages"]:
                    content = page["markdown"]
                    preview = content[:1200] + ("\n[preview truncated]" if len(content) > 1200 else "")
                    print(f"PAGE {page['page']} ({expected.filename}):\n{preview}", flush=True)
            results.append(result)
    print(f"\nPASS {len(results)} document(s) across {len(scenarios)} scenario(s)", flush=True)
    return results


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started = time.monotonic()
    failure: str | None = None
    results: list[CheckResult] = []
    try:
        results = workflow(args)
    except (CheckFailure, KeyboardInterrupt) as exc:
        failure = str(exc) or "interrupted"
        print(f"\nFAIL: {failure}", file=sys.stderr, flush=True)
    if args.report:
        report = {
            "mode": args.mode,
            "passed": failure is None,
            "duration_seconds": round(time.monotonic() - started, 3),
            "documents": [result.__dict__ for result in results],
            "error": failure,
        }
        try:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(f"Report saved: {args.report}", flush=True)
        except OSError as exc:
            print(f"FAIL: could not save report to {args.report}: {exc}", file=sys.stderr)
            return 1
    return 1 if failure is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run the actual zero-dependency workflow tester against a local fake HTTP API.

These tests exercise upload encoding, UUID capture, polling, pages, assertions,
negative paths and reports without Redis or paid OpenRouter access.
"""
from __future__ import annotations

import importlib.util
import io
import json
import threading
import uuid
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace

import pytest


PROJECT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT / "scripts" / "check_workflow.py"
SAMPLES = PROJECT / "samples"
spec = importlib.util.spec_from_file_location("check_workflow", SCRIPT)
assert spec and spec.loader
import sys
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class ScenarioHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, handler):
        super().__init__(server_address, handler)
        self.jobs: dict[str, dict] = {}
        self.uploads: list[tuple[str, list[str]]] = []
        self.error_upload = False
        self.unordered_pages = False
        self.empty_summary = False
        self.not_ready = False
        self.never_finish = False
        self.grouped_numbers = False


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def write_json(self, status: int, content: dict):
        raw = json.dumps(content).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/api/v1/health":
            return self.write_json(200, {"status": "ok"})
        if self.path == "/api/v1/ready":
            return self.write_json(503 if self.server.not_ready else 200,
                                   {"status": "down" if self.server.not_ready else "ready"})
        pieces = self.path.split("/")
        if len(pieces) < 5 or pieces[:4] != ["", "api", "v1", "documents"]:
            return self.write_json(404, {"error": {"code": "NOT_FOUND"}})
        identifier = pieces[4]
        job = self.server.jobs.get(identifier)
        if job is None:
            return self.write_json(404, {"error": {"code": "NOT_FOUND"}})
        if len(pieces) >= 6 and pieces[5] == "pages":
            if job["polls"] < 3 or job["terminal"] == "failed":
                return self.write_json(409, {"error": {"code": "RESULT_NOT_READY", "message": "not complete"}})
            return self.write_json(200, {"id": identifier, "pages": job["pages"]})
        job["polls"] += 1
        status = "queued" if job["polls"] == 1 else ("processing" if job["polls"] == 2 else job["terminal"])
        if self.server.never_finish:
            status = "processing"
        return self.write_json(200, {
            "id": identifier,
            "filename": job["filename"],
            "parser": job["parser"],
            "status": status,
            "summary": None if status != "completed" else ("" if self.server.empty_summary else job["summary"]),
            "page_count": len(job["pages"]) if status == "completed" else None,
            "error": {"code": "NO_EXTRACTABLE_TEXT", "message": "No text"} if status == "failed" else None,
        })

    def do_POST(self):
        if self.path != "/api/v1/documents":
            return self.write_json(404, {"error": {"code": "NOT_FOUND"}})
        if self.server.error_upload:
            return self.write_json(503, {"error": {"code": "UPLOAD_OUTCOME_UNKNOWN"}})
        content_type = self.headers.get("Content-Type")
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        message = BytesParser(policy=policy.default).parsebytes(
            b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode() + b"\r\n\r\n" + raw
        )
        fields = {}
        files = []
        for part in message.iter_parts():
            name = part.get_param("name", header="Content-Disposition")
            if name == "parser":
                fields[name] = part.get_payload(decode=True).decode()
            elif name == "files":
                filename = part.get_filename()
                assert part.get_payload(decode=True).startswith(b"%PDF")
                files.append(filename)
        parser = fields["parser"]
        self.server.uploads.append((parser, files))
        accepted = []
        for filename in files:
            uid = str(uuid.uuid4())
            if filename == "blank_middle_page.pdf":
                pages = [{"page": 1, "markdown": "First page: a distinct sentence."},
                         {"page": 2, "markdown": ""},
                         {"page": 3, "markdown": "Third page: closing remarks."}]
                summary = "The first and last page are present"
            elif filename == "table_and_headings.pdf":
                amount = "12,000" if self.server.grouped_numbers else "12000"
                pages = [{"page": 1, "markdown": f"# Quarterly Revenue Report\n| Hardware | {amount} |"},
                         {"page": 2, "markdown": "Summary and Findings"}]
                summary = "A revenue report with earnings"
            elif filename == "scanned_image_only.pdf":
                pages = [{"page": 1, "markdown": "# SCANNED BLUE ORCHID"}]
                summary = "The scanned BLUE ORCHID document"
            else:
                pages = [{"page": 1, "markdown": "First page: river flows to the sea"},
                         {"page": 2, "markdown": "Second page: results are positive"}]
                summary = "Document Alpha shows a positive result"
            if self.server.unordered_pages:
                pages[0]["page"] = 2
            self.server.jobs[uid] = {
                "filename": filename, "parser": parser, "pages": pages, "summary": summary,
                "polls": 0, "terminal": "failed" if filename == "scanned_image_only.pdf" and parser == "pypdf" else "completed",
            }
            accepted.append({"id": uid, "filename": filename, "parser": parser, "status": "queued"})
        return self.write_json(202, {"documents": accepted})


@pytest.fixture
def server():
    httpd = ScenarioHTTPServer(("127.0.0.1", 0), MockHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(server=httpd, url=f"http://127.0.0.1:{httpd.server_port}")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def run_cli(*options):
    output, errors = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        code = module.main(list(options))
    return code, output.getvalue(), errors.getvalue()


def test_check_readiness_does_not_upload(server):
    code, out, err = run_cli("--mode", "check", "--base-url", server.url)
    assert code == 0, err
    assert "no files uploaded" in out
    assert server.server.uploads == []


def test_quick_upload_auto_id_poll_and_validate(server):
    code, out, err = run_cli("--mode", "quick", "--base-url", server.url, "--poll", "0.001")
    assert code == 0, err
    assert "PASS 1 document" in out
    assert server.server.uploads == [("pypdf", ["basic_text.pdf"])]
    assert all(job["polls"] >= 3 for job in server.server.jobs.values())


def test_full_covers_multi_upload_all_parsers_blank_and_expected_failure(server):
    code, out, err = run_cli("--mode", "full", "--base-url", server.url, "--poll", "0.001")
    assert code == 0, err
    assert "PASS 5 document" in out
    assert len(server.server.uploads) == 4
    assert server.server.uploads[0] == ("pypdf", ["basic_text.pdf", "blank_middle_page.pdf"])
    assert ("gemini", ["table_and_headings.pdf"]) in server.server.uploads
    assert ("gpt5_mini", ["scanned_image_only.pdf"]) in server.server.uploads
    assert "expected failure" in out


def test_custom_checks_specific_content_and_generates_report(server, tmp_path):
    report = tmp_path / "result.json"
    code, out, err = run_cli("--mode", "custom", "--base-url", server.url,
                             "--file", str(SAMPLES / "basic_text.pdf"), "--parser", "pypdf",
                             "--expect-text", "river flows to the sea", "--report", str(report),
                             "--poll", "0.001")
    assert code == 0, err
    payload = json.loads(report.read_text())
    assert payload["passed"] is True and len(payload["documents"]) == 1
    assert payload["documents"][0]["status"] == "completed"
    assert "summary" not in payload["documents"][0]


def test_custom_expected_failure(server):
    code, out, err = run_cli("--mode", "custom", "--base-url", server.url,
                             "--file", str(SAMPLES / "scanned_image_only.pdf"),
                             "--expect-error", "NO_EXTRACTABLE_TEXT", "--poll", "0.001")
    assert code == 0, err
    assert "expected failure" in out


def test_no_automatic_retry_when_commit_outcome_unknown(server):
    server.server.error_upload = True
    code, out, err = run_cli("--mode", "quick", "--base-url", server.url)
    assert code == 1
    assert "UPLOAD_OUTCOME_UNKNOWN" in err and "Do not blindly retry" in err
    assert len(server.server.uploads) == 0


def test_rejects_out_of_order_page_numbers(server):
    server.server.unordered_pages = True
    code, out, err = run_cli("--mode", "quick", "--base-url", server.url, "--poll", "0.001")
    assert code == 1 and "page numbering/order" in err


def test_rejects_completed_without_summary(server):
    server.server.empty_summary = True
    code, out, err = run_cli("--mode", "quick", "--base-url", server.url, "--poll", "0.001")
    assert code == 1 and "without a nonempty summary" in err


def test_readiness_failure_prevents_upload(server):
    server.server.not_ready = True
    code, out, err = run_cli("--mode", "quick", "--base-url", server.url)
    assert code == 1 and "expected HTTP 200" in err
    assert server.server.uploads == []


def test_timeout_is_bounded(server):
    server.server.never_finish = True
    code, out, err = run_cli("--mode", "quick", "--base-url", server.url,
                             "--timeout", "0.02", "--poll", "0.001")
    assert code == 1 and "timed out" in err


def test_custom_expected_text_missing_is_failure(server):
    code, out, err = run_cli("--mode", "custom", "--base-url", server.url,
                             "--file", str(SAMPLES / "basic_text.pdf"),
                             "--expect-text", "never in this document", "--poll", "0.001")
    assert code == 1 and "extracted pages missing" in err


def test_script_is_copied_into_docker_image():
    dockerfile = (PROJECT / "backend" / "Dockerfile").read_text()
    # The container must preserve the same layout as the host, otherwise
    # tests/test_workflow_cli.py resolves /scripts instead of /app/scripts.
    assert "WORKDIR /app\nCOPY backend/ ./backend/" in dockerfile
    assert "COPY samples/ ./samples/" in dockerfile
    assert "COPY scripts/ ./scripts/" in dockerfile
    assert "COPY docker-compose.yml ./docker-compose.yml" in dockerfile
    assert "WORKDIR /app/backend" in dockerfile


def test_setup_checks_lockfile_and_creates_no_paid_jobs():
    shell = (PROJECT / "scripts" / "setup.sh").read_text()
    assert "lock_dependencies.sh" in shell
    assert "--mode check" in shell
    assert "--mode quick" not in shell.split("# One-command Linux bootstrap")[0]


def test_failed_report_contains_error_without_document_contents(server, tmp_path):
    server.server.error_upload = True
    report = tmp_path / "failed.json"
    code, out, err = run_cli("--mode", "quick", "--base-url", server.url, "--report", str(report))
    assert code == 1
    result = json.loads(report.read_text())
    assert result["passed"] is False
    assert "UPLOAD_OUTCOME_UNKNOWN" in result["error"]
    assert "markdown" not in result and "summary" not in result


def test_gemini_accepts_grouped_financial_number(server):
    # Fixture's revenue figure may appear as either 12000 or 12,000.
    server.server.grouped_numbers = True
    code, out, err = run_cli("--mode", "full", "--base-url", server.url, "--poll", "0.001")
    assert code == 0, err


def test_show_results_prints_summary_and_pages(server):
    code, out, err = run_cli("--mode", "quick", "--base-url", server.url,
                             "--show-results", "--poll", "0.001")
    assert code == 0, err
    assert "SUMMARY basic_text.pdf" in out
    assert "PAGE 1 (basic_text.pdf)" in out
    assert "PAGE 2 (basic_text.pdf)" in out


def test_compose_exposes_demo_api_on_loopback_only():
    compose = (PROJECT / "docker-compose.yml").read_text()
    assert '"127.0.0.1:8000:8000"' in compose

# Optional OpenRouter model benchmarks — current tooling

The benchmark is independent from the production FastAPI application and **does not change production models**. It compares a curated list of 15 model IDs, not every OpenRouter model. Live catalog filtering skips unavailable or modality-incompatible candidates.

## One command — full real benchmark

```bash
./scripts/benchmark.sh all --execute
```

Run as your normal Linux user, from the repository root, with `OPENROUTER_API_KEY` set in `.env`. The Docker wrapper builds its Python image, generates 36 synthetic PDFs, validates the current OpenRouter catalog, tests each supported model/route, evaluates results, and creates a combined report. The command sends **paid** OpenRouter requests.

**No in-run spending cap.** Your OpenRouter API key/provider determines your credit limit. The runner records `usage.cost` when returned; unreported costs are `null`/unknown, not `$0`. It **continues after HTTP errors, timeouts, incomplete `finish_reason=length` outputs, and missing usage**. It does not automatically retry a failed request. If an image route returns HTTP 404, remaining requests to that model *in that route* are marked unavailable without additional API calls; other models and routes continue. If provider credit is exhausted, subsequent requests may fail, but the report still records the attempts. API failures can still incur costs that OpenRouter does not report.

`--max-budget-usd` is accepted for CLI compatibility but **ignored**. `--max-requests` is still available as an **explicit optional** planning limit if you deliberately want fewer calls, but there is no default request limit for the full suite.

## Other one-line actions

```bash
./scripts/benchmark.sh all --dry-run                 # Generate PDFs; no paid calls
./scripts/benchmark.sh all --mock                    # End-to-end offline simulation (not model accuracy)
./scripts/benchmark.sh all --profile screening --execute # Smaller real comparison, all configured models
./scripts/benchmark.sh test                          # Offline benchmark tests
```

No `sudo` is required. The wrapper uses your host UID/GID on the benchmark bind mount and does not require the other application services running. Rootless Docker Engine is a separate, planned improvement.

## Test corpus

There are **12 scenarios × 3 variants = 36 synthetic documents**, with known ground truth and SHA-256 identifiers. `clean` is digital text-layer PDF; `scan` is raster-only (196 DPI); `hard` is degraded raster (108 DPI and artifacts). Scenarios: `letter`, `invoice`, `quarterly`, `newsletter`, `inventory` (3 pages), `lowres`, `skew`, `receipt`, `bilingual` (English/Cyrillic), `serial` (O/0/I/1/l), `injection`, and `project` (2 pages). Generated fixtures are deterministic for seed `20261112`; data contains no real customer documents.

The full suite tests all scenarios for PyPDF (all three variants), image OCR (scan/hard), native PDF (scan/hard), the Mistral PDF-parser + language-model pipeline (scan/hard), and summary (clean source). The screening profile uses invoice/serial/bilingual/receipt for OCR and letter/quarterly/project/invoice for summary. Different input routes **must not** be treated as directly equivalent.

## Results

Each full run creates a fresh `benchmarks/results/suite-<timestamp>/` directory containing:

```text
selection.html       # Open this first: route links, aggregate comparison, reported costs
comparison.csv       # All route/model statistics
suite.json           # Coverage, failed requests, skipped models, recorded spend, stage warnings
pypdf/report.html
image/report.html
pdf_native/report.html
pdf_mistral/report.html
summary/report.html
```

Stage folders also include `responses.jsonl` (including errors and usage), `run.json`, `details.csv` and `comparison.csv`. A missing/nonparticipating route is noted in the suite. Costs reflect **reported successful and failed-call usage** where OpenRouter provides it; no estimate is inserted for missing usage. Exact-value error rates, character/word error rates, tables, reading-order, latency, cost and reliability are shown. Scores and prices are reported per route; a model with excellent scores on one successful page is not a validated winner.

Summary quality checks use literal anchor recall rather than a semantic factuality judge. Read output examples to inspect hallucinations, contradictions and OCR identifier mistakes before changing production defaults. Mistral route measures parser+model combined costs/results, not isolated Mistral OCR.

## Fine-grained commands

```bash
./scripts/benchmark.sh generate
./scripts/benchmark.sh run --route image --models gemini_current,ling_vl \
  --cases invoice,serial,bilingual --variants scan,hard --execute \
  --output-dir benchmarks/results/image-pilot
./scripts/benchmark.sh evaluate --run-dir benchmarks/results/image-pilot
```

Use `--resume` with the **same input parameters** and result directory to avoid repeating completed units in an interrupted individual route run. The resume comparison ignores deprecated spend/request caps but checks dataset, model list, prompt version, variants, cases, repeats and output token cap. Failed units are not automatically retried. The `all` command always creates a new suite directory, so repeated `all --execute` runs issue new requests.

Do not publish API keys or `.env`. `benchmarks/results/` is Git-ignored. For exact commands and fixture behavior, see the benchmark scripts and tests in this directory. Results from real model calls are not part of the default CI gate.

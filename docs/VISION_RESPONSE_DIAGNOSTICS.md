# Diagnosing invalid AI OCR + Pictures responses

OpenRouter HTTP status 200 does not mean the model's `choices[0].message.content` is valid JSON. `VISION_RESPONSE_INVALID` comes from **application-level** validation of the model-generated content, including Markdown/figure marker consistency. The backend requests a JSON object but currently does not enforce OpenRouter's structured-output `response_format` feature.

## Temporary full-response diagnostic mode

For a locally controlled debugging session, set in the **server's** `.env`:

```dotenv
VISION_LOG_INVALID_RESPONSE=true
```

Recreate the worker containers **without** removing volumes:

```bash
docker compose up -d --no-deps --force-recreate worker
docker compose logs --no-log-prefix --follow worker | grep --line-buffered 'vision.response.invalid.raw'
```

Run AI OCR + Pictures on the problematic document. Each rejected attempt creates one JSON log event (`vision.response.invalid.raw`) with:

- `page`, `vision_attempt`, `error_code`
- `vision_raw_model_content` — the exact model-generated text, **not truncated**
- `vision_raw_http_response` — the complete original OpenRouter HTTP body for live calls; absent on replay from older archived attempts
- `vision_json_decode_error` — JSON position/line/column if parsing failed
- `vision_finish_reason` — the OpenRouter completion finish reason when available

The normal log message may be truncated to 2,000 characters; these special response fields are **not**. Do not paste the full records into public GitHub issues.

**Privacy warning:** full responses can contain complete PDF text, personally identifiable information, and other confidential data. Although direct OTLP log export skips this event, Docker stdout can be collected by external telemetry/logging software. Avoid enabling it when the host ships container logs to third parties. Docker rotates logs; remove or restrict diagnostic copies appropriately.

Once you have diagnosed the issue, restore the default:

```dotenv
VISION_LOG_INVALID_RESPONSE=false
```

Recreate workers with `docker compose up -d --no-deps --force-recreate worker`.

Do **not** use `docker compose down -v`: it deletes storage volumes.

## Interpreting the output

- `vision_raw_model_content` begins with ```` ```json ````: the model supplied Markdown code fences instead of a bare JSON object.
- Content ends mid-string or mid-array, even with `finish_reason=stop`: the model stopped before completing the expected payload.
- JSON parses but figure markers differ from `figures`: schema-level disagreement between the generated Markdown and its figure records.
- `vision_raw_http_response` is valid JSON, while `vision_raw_model_content` is not: this is a **model-output format** issue, not an OpenRouter JSON protocol problem.

The worker retries clearly rejected responses up to `VISION_FIGURES_PAGE_ATTEMPTS=3`. A network timeout or missing recorded response has uncertain billing and is **not** automatically retried. Validated pages can be reused across processing runs for the same PDF and configuration.

## Inspect previously saved failed responses (no new AI requests)

For attempts whose responses were already recorded in RustFS, you can inspect the stored model-generated text without making another paid request. If you know the **processing run UUID** (not the PDF SHA-256), inspect its stored response directly:

```bash
# Do not paste the output into a public ticket: it may be the whole PDF page.
docker compose exec -T worker python -m app.vision_response_cli RUN_UUID 8
```

The CLI scans archive manifests to resolve the run's SHA-256. You can avoid that scan with `--document-id ORIGINAL_PDF_SHA256`. On a new diagnostic-enabled run, append `--attempt 2` or `--attempt 3` to read a retry's response. The command reads RustFS objects only; **it does not contact OpenRouter or reprocess the PDF**. It returns the stored model text verbatim; earlier versions did not archive the full OpenRouter HTTP envelope.

# Live extraction page progress

The `GET /api/v1/documents/{run_id}` endpoint may return `page_progress` while a run is in `processing` state:

```json
{"status":"processing","page_progress":{"page":5,"pages_total":21,"attempt":2,"max_attempts":3}}
```

The Upload workspace and archived active-run view display **Extracting page 5 of 21 (retry 2/3)…** and update through the existing two-second status polling. A retry indicator appears only for `vision_figures` attempts 2 or 3. Gemini and GPT-5 Mini report page numbers without retries; fast local PyPDF processing uses a generic extraction status.

The page number means **currently extracting that page**, not that the page has completed or that a fixed percentage of work remains. Successful page responses and cache hits can advance the counter quickly; costly model calls may leave the counter stationary for longer.

Progress lives in **Redis metadata**, fenced to the owning worker, and is not written to the permanent RustFS manifest on every page. It may be temporarily absent after a Redis restart or during worker takeover. The status API omits it outside `processing` and the UI falls back to the generic message when it is unavailable. It is not a durable accounting of already completed pages or billed API calls.

No LLM requests are made by status polling.

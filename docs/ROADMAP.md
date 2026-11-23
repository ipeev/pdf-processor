# Potential roadmap — post-0.12.3

This roadmap lists **possible future improvements**, not features in the current release or committed delivery dates. The current baseline and limitations are described in [project overview](../project_overview.md). Prior prototype/phase plans are intentionally not retained in the public documentation.

## 1. Figure and OCR accuracy

**Goal:** improve Markdown/figure fidelity without regressing current archive semantics.

- Better figure segmentation and crop confidence for charts with axes, panels, and captions; reduce caption fragments inside PNGs.
- Stronger fallback placement for figures with missing/incorrect captions, multi-column layouts, and cross-page captions.
- More comprehensive automatic evaluation on synthetic, redistributable scans, mathematical papers, plots, and tables, with manual visual acceptance guidelines.
- Detect incomplete or truncated full-page AI responses more reliably; communicate uncertainty rather than silently claiming complete text.
- Explore table-image fallbacks only where text extraction genuinely fails, with explicit output semantics.

## 2. Production security / accessibility

**Goal:** define a deployable trust boundary for users beyond a trusted localhost machine.

- Authentication, per-user authorization, robust tenant isolation, and audit logging.
- TLS termination, CSRF/cookie strategy where applicable, secure secret storage, and safe external routing.
- Harden OpenSearch, RustFS, and API exposure under a documented production threat model.
- Improve keyboard/screen-reader coverage and accessibility testing for image/results workflows.

**Current status:** not implemented. Do not expose this release publicly.

## 3. Operations and scaling

- Documented disaster-recovery rehearsals with validated restore integrity and backup retention.
- Safer multi-host/distributed coordination replacing local `flock`, with a clear migration design.
- Better resource tuning for large batches/scans and backpressure on expensive AI calls.
- Observability installation examples and reliable diagnostics that do not leak document contents.
- CI expansion for security, realistic performance, and image fidelity using free, synthetic fixtures.

## 4. Developer experience

- More portable deployment templates after securing the runtime boundary.
- Expand schema-backed API examples, OpenAPI documentation, and client-generation tests.
- Improve release automation, repository metadata, screenshots from synthetic datasets, and dependency/license compliance checks.
- Provide clearer error guidance for unavailable AI routes and provider limits.

## Definition of done for future changes

A future feature should include an explicit API/data-model impact, offline tests with no provider key, deterministic fixtures, safe migration/backward-compatibility expectations, a security review, and documentation updates. Live paid evaluation remains opt-in and separate from public CI.

# Security policy

## Supported deployment boundary

This is a **development project for a single trusted user on localhost**. It is not designed for public internet exposure. There is currently no application-level authentication/multitenant authorization or TLS termination in this repository. OpenSearch has its security plugin disabled on an internal Docker network; demonstration S3 credentials in `.env.example` are not production credentials.

Keep `127.0.0.1` Docker port bindings, change example S3 credentials, use a private tunnel for remote access, and secure the host's Docker daemon. Do not process PDFs that you are not authorized to store and send to configured AI providers. Review the provider's privacy/cost terms before using paid extraction.

**Never publish** `.env`, API keys, Docker volume dumps, real document contents, raw provider responses, telemetry ingest tokens, or logs with personal identifiers. GitHub Actions deliberately has no provider key and must remain offline.

## Report a vulnerability

Use GitHub's **private vulnerability reporting** feature if enabled under the repository's Security settings, or contact the maintainer through a private channel listed in the published GitHub profile. Please do not place exploit steps or private files in public issues. No security response SLA is promised for this development release.

The public release's own CI/CodeQL and secret scan are useful checks, but do not substitute for a professional security assessment.

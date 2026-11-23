# Python 3.15.2 is selected by the base image. uv installs dependencies
# locked by the bundled backend/uv.lock (missing lock fails the build).
FROM python:3.15.2-slim-trixie

RUN python -c "import sys; assert sys.version_info[:3] == (3, 15, 2), sys.version"

# Synthetic benchmark PDF generation uses DejaVu for reproducible Cyrillic
# and monospace fixtures. Install fonts in the image, not the repository.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 pdfapp \
    && useradd --uid 10001 --gid pdfapp --home-dir /tmp --no-create-home --shell /usr/sbin/nologin pdfapp

COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    HOME=/tmp \
    UV_CACHE_DIR=/tmp/uv-cache \
    PATH="/app/backend/.venv/bin:$PATH"

# Keep the SAME directory structure as the host repository.
# In particular, pytest test_workflow_cli.py and test_uv_tooling.py resolve
# repository-relative paths using Path(__file__).resolve().parents[2].
WORKDIR /app
COPY backend/ ./backend/
# Metadata only for stable-release offline lock-preflight (no React source or npm modules).
COPY frontend/package.json frontend/package-lock.json ./frontend/
COPY samples/ ./samples/
COPY scripts/ ./scripts/
COPY docker-compose.yml ./docker-compose.yml

# Run uv in the Python project directory (not the repository root).
WORKDIR /app/backend

# Use the bundled lockfile; never resolve dependencies implicitly.
# Build-time uv cache must not be baked into the unprivileged runtime image.
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    export UV_CACHE_DIR=/root/.cache/uv; \
    test -s uv.lock \
    && uv sync --locked --extra test --no-editable \
    && chown -R 10001:10001 /app/backend

# The application, worker and ad hoc test commands never run as root.
# /tmp stays writable for streaming uploads, page renders, uv and pytest.
USER 10001:10001

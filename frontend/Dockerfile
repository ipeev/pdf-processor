# Build and test React entirely inside Docker. The Nginx runtime has no Node dependencies.
# Use glibc-based Node for native Rollup tooling; runtime remains Alpine Nginx.
FROM node:26-bookworm-slim AS builder
WORKDIR /workspace
# Browser-facing settings (injected at build time; not Docker-internal network addresses).
ARG VITE_GRAFANA_URL
ARG VITE_GRAFANA_TEMPO_UID
ENV VITE_GRAFANA_URL=${VITE_GRAFANA_URL}
ENV VITE_GRAFANA_TEMPO_UID=${VITE_GRAFANA_TEMPO_UID}
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
# Print the resolved build toolchain for diagnosis; do not skip the test/build gate.
RUN node --version && npm --version && npm ls react react-dom vite rollup @vitejs/plugin-react --all
COPY frontend/tsconfig.json frontend/vite.config.ts frontend/index.html ./
COPY frontend/src ./src
# Version-consistency test reads ../../backend/pyproject.toml from /workspace/tests.
# Copy only package metadata into this temporary builder (not the Nginx runtime).
COPY backend/pyproject.toml /backend/pyproject.toml
# Parser contract test reads the backend enum during npm test.
COPY backend/app/schemas.py /backend/app/schemas.py
COPY frontend/tests ./tests
RUN npm test && npm run build

FROM nginx:1.31-alpine
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /workspace/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s CMD wget -q -O /dev/null http://127.0.0.1/ || exit 1

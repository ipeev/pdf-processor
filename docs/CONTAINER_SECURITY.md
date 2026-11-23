# Container security and host filesystem boundaries

The current Docker Compose deployment uses unprivileged API/worker processes (UID/GID 10001), Linux capability dropping and `no-new-privileges`. The application is intended for a trusted, single-user Linux host, **not a public internet service**. See [SECURITY.md](../SECURITY.md).

## Volumes and permissions

- API and worker services have no arbitrary host-directory bind mounts. Canonical PDF originals and results are stored in the Docker-managed `rustfs_data` named volume.
- Redis uses ephemeral state and has no durable snapshot or append-only log. OpenSearch has its own named data volume; the archive remains authoritative.
- A narrowly scoped one-shot `rustfs-permissions` initializer runs as root to set ownership of Docker-managed volume roots. It has no network, a read-only root filesystem, and only the CHOWN Linux capability. API/workers remain unprivileged.
- The archive coordination volume contains a shared advisory `flock` file. This coordinates cooperating containers on a **single host**; it is not a distributed lock, access-control mechanism or authentication system.
- The optional benchmark wrapper mounts only `benchmarks/`, runs under the invoking host UID/GID, and refuses a root-invoked workflow.

## Host security restrictions

A conventional rootful Docker daemon is privileged. Membership in the Docker group can grant host-root-equivalent access. The project container hardening does **not** make Docker itself safe for untrusted users. Rootless Docker or user namespaces are host-level decisions and are not guaranteed by this release.

OpenSearch security is disabled within its internal Docker network. Redis and OpenSearch do not publish host ports. The frontend/API/RustFS published ports bind to `127.0.0.1`; no TLS gateway or app-level user authentication is included. Use trusted access only.

## Verification

```bash
./scripts/test_release.sh --offline
docker compose exec -T api id
docker compose exec -T worker id
```

Both app processes should report UID 10001, not 0. Use [Operations](OPERATIONS.md) for safe backups and restarts; **never run `docker compose down -v` unless all named-volume data may be destroyed**.

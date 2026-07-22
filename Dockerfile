# Henk agent image. Runs as a non-root user, no host mounts beyond its config.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HENK_CONFIG=/app/config.yaml

RUN useradd --create-home --uid 10001 henk
WORKDIR /app

# Install deps first for layer caching, then the package (with the runtime extra:
# claude-agent-sdk + websockets).
COPY pyproject.toml README.md ./
COPY henk ./henk
RUN pip install ".[runtime]"

# Create the audit mount point owned by henk BEFORE dropping privileges. When the
# empty henk_audit named volume first mounts here Docker seeds it from the image,
# so the volume inherits henk ownership and the non-root process can write to it
# (otherwise the volume is root-owned and every audit write fails, silently).
RUN mkdir -p /data/audit && chown -R 10001:10001 /data

USER henk
CMD ["python", "-m", "henk"]

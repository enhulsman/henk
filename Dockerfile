# Henk agent image. Runs as a non-root user, no host mounts beyond its config.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HENK_CONFIG=/app/config.yaml

# Empty TZPATH: make the pinned `tzdata` wheel the ONLY zone-database source.
# Declaring the dependency guarantees availability but not precedence — `zoneinfo`
# searches TZPATH first and consults the wheel only on a miss — so a base image
# carrying a stale or trimmed zone tree wins silently. Silence is the failure mode
# here: an ABSENT database raises at first resolution (loud), a STALE one resolves
# to the wrong offset (not). With TZPATH empty the zone database moves when a
# reviewed dependency bump moves it, not when a base image is rebuilt. Verified:
# with the wheel and TZPATH empty, 598 zones resolve and `localtime` is no longer
# among them; without the wheel, the first resolution raises ZoneInfoNotFoundError
# rather than quietly reaching for something older. A trimmed system tree is not
# hypothetical — a development host was measured at 498 zones with US/Eastern and
# Europe/Kiev missing entirely, which is the exact shape of the bug this forecloses.
#
# Its own ENV instruction, not folded into the block above: a comment inside a
# line continuation is easy to get subtly wrong and this value is load-bearing.
ENV PYTHONTZPATH=""

# The process default zone, so production is at least DETERMINISTIC if the suite's
# process-timezone guard is ever removed. The guard is the real defence — nothing
# in resolution or rendering may read the process zone at all — and this is only
# the floor under it.
ENV TZ=UTC

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

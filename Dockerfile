# Blast Radius entry. Built for linux/amd64; runs with no network.
#
# The base image is pinned by digest, and it is exactly the digest the starter
# kit pins: the organisers labeled every commit in this image, so its Python and
# SQLite are what decided which sqlglot tests ran. Changing it would change the
# ground truth under us.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# No third-party packages: the selector is pure Python standard library, which
# keeps interpreter startup -- and therefore our scored CPU seconds -- minimal.

# Create /app explicitly as world-traversable, then copy the selector and make
# it world-readable, so it is readable by the unprivileged runtime user
# (uid 65534 / nobody) and not only by root. Do not fold the mode into
# `COPY --chmod`: BuildKit applies that mode to the parent directory it creates
# too, which leaves /app without the +x needed to traverse into it.
RUN mkdir -p /app && chmod 0755 /app
COPY selector.py /app/selector.py
RUN chmod 0444 /app/selector.py

# Run unprivileged. The scorer also passes --user 65534:65534; setting it here
# means the image is correct on its own and never depends on root.
USER 65534:65534

# HOME=/tmp matches the scorer, which mounts a 64 MB tmpfs there. Everything
# else is read-only, so suppress bytecode writing (-B / PYTHONDONTWRITEBYTECODE)
# instead of letting Python attempt a write that cannot succeed.
ENV HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["python", "-B", "/app/selector.py"]

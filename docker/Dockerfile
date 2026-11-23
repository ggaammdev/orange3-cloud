# The generic orange3-cloud machine image.
#
# It contains only uv, CA certificates, a C/C++ compiler (for packages that
# have no wheel for the machine's architecture), a small Python that runs
# bootstrap.py, and bootstrap.py itself (with boot_platforms.py). When the container starts, bootstrap.py
# installs the user's exact Python and packages and then starts the API server
# (see the docstring of docker/bootstrap.py).
#
# Build (from the repository root; supports linux/amd64 and linux/arm64):
#   docker buildx build -f docker/Dockerfile -t orange3-cloud-machine .
# Development images bundle the add-on wheel built from the same commit:
#   docker buildx build -f docker/Dockerfile --build-context wheels=dist/ ...
# The wheel is installed when its version matches the user's orange3-cloud.

# Empty unless a "wheels" build context is given (see above).
FROM scratch AS wheels

FROM ghcr.io/astral-sh/uv:0.13.9 AS uv

# Pinned by digest, so Dependabot proposes each new Debian base image.
FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2

ARG BOOTSTRAP_PYTHON=3.13

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gcc g++ libc6-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/

# Python for bootstrap.py only, outside the cache volume. The user's Python is
# installed at start-up into the cache volume.
RUN UV_PYTHON_INSTALL_DIR=/opt/orange3-cloud/bootstrap-python \
        uv python install --no-cache "$BOOTSTRAP_PYTHON" \
    && ln -s "$(UV_PYTHON_INSTALL_DIR=/opt/orange3-cloud/bootstrap-python \
        uv python find --python-preference only-managed "$BOOTSTRAP_PYTHON")" \
        /usr/local/bin/bootstrap-python \
    && bootstrap-python --version

RUN useradd --create-home --uid 1000 orange \
    && mkdir -p /var/cache/orange3-cloud /var/lib/orange3-cloud /opt/orange3-cloud/wheels \
    && chown orange:orange /var/cache/orange3-cloud /var/lib/orange3-cloud

COPY --from=wheels / /opt/orange3-cloud/wheels/
COPY docker/bootstrap.py docker/boot_platforms.py /opt/orange3-cloud/

ENV UV_CACHE_DIR=/var/cache/orange3-cloud/uv \
    UV_PYTHON_INSTALL_DIR=/var/cache/orange3-cloud/python \
    UV_NO_PROGRESS=1 \
    PYTHONUNBUFFERED=1

# uv's download cache, Python installs and the virtual environment: kept on
# disk so the next start doesn't download everything again.
VOLUME /var/cache/orange3-cloud

USER orange
WORKDIR /home/orange
EXPOSE 8443

ENTRYPOINT ["bootstrap-python", "/opt/orange3-cloud/bootstrap.py"]

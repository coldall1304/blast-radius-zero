# Blast Radius Zero sandbox image.
#
# The control plane runs this image as:
#   docker run --rm --network none --read-only --user 65534:65534 \
#     --cap-drop ALL --security-opt no-new-privileges:true ... \
#     brz-sandbox:1 python -I -
#
# So: no daemon, no shell entrypoint, no writable paths outside /tmp and the
# per-step /out bind mount, and no user account beyond the built-in nobody.
FROM python:3.12-slim

LABEL org.opencontainers.image.title="brz-sandbox" \
      org.opencontainers.image.description="Untrusted Python execution cell for Blast Radius Zero" \
      org.opencontainers.image.version="1"

# Runtime libraries only. Wheels carry their own BLAS/LAPACK; libgomp is needed
# by numpy's OpenMP path, and freetype/png by matplotlib's font rendering.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        libgomp1 \
        libfreetype6 \
        libpng16-16 \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /tmp/*

# Pinned so a rebuild months later still produces the same interpreter surface.
RUN pip install --no-cache-dir \
        "numpy==2.5.4" \
        "pandas==3.1.0" \
        "matplotlib==3.11.2" \
 && find /usr/local/lib/python3.12 -name '__pycache__' -type d -prune -exec rm -rf {} + \
 && rm -rf /root/.cache/pip

# Everything the cell is allowed to touch. /out is bind-mounted per step by the
# control plane; 0777 keeps it writable for arbitrary host uids and for nobody.
RUN mkdir -p /out && chmod 0777 /out

# --read-only leaves no writable /tmp unless the caller adds a tmpfs, so the
# image-level matplotlib cache default lives on /dev/shm (a tmpfs Docker always
# mounts, even read-only). The control plane overrides MPLCONFIGDIR=/tmp/mpl and
# supplies the tmpfs itself.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONNOUSERSITE=1 \
    PYTHONHASHSEED=0 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/dev/shm/mpl \
    HOME=/tmp \
    OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    MALLOC_ARENA_MAX=2 \
    PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /out

# Built-in nobody (65534:65534) only. --user on the command line must match.
USER 65534:65534

# Placeholder: the control plane feeds code on stdin to `python -I -`.
CMD ["python", "-c", "print('brz-sandbox placeholder: run `python -I -` with code on stdin')"]

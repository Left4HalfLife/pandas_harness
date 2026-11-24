FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SCARF_NO_ANALYTICS=true \
    MPLCONFIGDIR=/tmp/matplotlib \
    HOME=/tmp \
    OPENBLAS_NUM_THREADS=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 harness \
    && useradd --uid 10001 --gid harness --no-create-home harness \
    && mkdir /jobs \
    && chown harness:harness /jobs

COPY --chown=harness:harness harness ./harness
COPY --chown=harness:harness worker.py wsgi.py ./
USER harness
EXPOSE 5000
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "--timeout", "180", "wsgi:app"]

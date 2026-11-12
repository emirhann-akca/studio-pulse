# Studio Pulse collector for Cloud Run (cloud mode: BigQuery Storage Write API).
# Built from the repository root:  docker build -f deploy/cloudrun/Dockerfile .
# Not built or deployed by this repository's CI; see deploy/cloudrun/README.md.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PULSE_COLLECTOR_MODE=bigquery \
    PULSE_TRUSTED_PROXY_HOPS=1 \
    PULSE_DATA_DIR=/srv/pulse

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY models ./models
COPY alerts.yaml checks.yaml ./
RUN pip install ".[cloud,geoip]" && rm -rf /src

# Optional: bake DB-IP Country Lite (CC BY 4.0, checksum-verified) into the image.
ARG GEOIP=0
RUN mkdir -p /srv/pulse && if [ "$GEOIP" = "1" ]; then pulse collect setup-geoip --data-dir /srv/pulse; fi

RUN useradd --system --uid 10001 pulse && chown -R pulse /srv/pulse
USER pulse
WORKDIR /srv/pulse
# Cloud Run sets $PORT.
CMD ["sh", "-c", "exec pulse collect --host 0.0.0.0 --port ${PORT:-8080}"]

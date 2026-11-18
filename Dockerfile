# One image for the web app and the background job. Config and the SQLite DB live on a
# mounted volume (/data); nothing secret is baked in.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC \
    TCGWATCH_CONFIG=/data/config.yaml

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

RUN useradd --create-home --uid 10001 tcgwatch && mkdir /data && chown tcgwatch /data
USER tcgwatch
VOLUME /data
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["tcgwatch", "web-health", "--port", "8765"]

# Bind 0.0.0.0 inside the container; compose publishes the port on the host's loopback only.
ENTRYPOINT ["tcgwatch"]
CMD ["web", "--host", "0.0.0.0", "--port", "8765"]

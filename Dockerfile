FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OMNIARR_CONFIG=/config \
    OMNIARR_PORT=8484

# unar: unpacks RAR'd scene releases · tini: proper signal handling
RUN apt-get update \
 && apt-get install -y --no-install-recommends unar tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY omniarr ./omniarr
COPY definitions ./definitions
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8484
VOLUME ["/config"]
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8484/health', timeout=5)"

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["python", "-m", "omniarr"]

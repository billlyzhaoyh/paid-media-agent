# Self-hosted API and Slack worker with native PDF dependencies.

# Debian has no IBM Plex package. Pinned upstream releases (SIL OFL 1.1) supply the report fonts.
FROM python:3.13-slim AS fonts
ADD --checksum=sha256:fb365d910566e6d199cc2c15579a7dd9a267128e18431a394ed81f1970c69200 \
    https://github.com/IBM/plex/releases/download/%40ibm%2Fplex-sans%401.1.0/ibm-plex-sans.zip /tmp/
ADD --checksum=sha256:4bfc936d0e1fd19db6327a3786eabdbc3dc0d464500576f6458f6706df68d26c \
    https://github.com/IBM/plex/releases/download/%40ibm%2Fplex-mono%401.1.0/ibm-plex-mono.zip /tmp/
RUN python -m zipfile -e /tmp/ibm-plex-sans.zip /tmp/plex \
    && python -m zipfile -e /tmp/ibm-plex-mono.zip /tmp/plex \
    && mkdir -p /fonts \
    && find /tmp/plex -path '*/fonts/complete/ttf/*.ttf' ! -name '*Italic*' -exec cp {} /fonts/ \; \
    && cp /tmp/plex/ibm-plex-sans/LICENSE.txt /fonts/LICENSE.txt

FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libffi8 libcairo2 libgdk-pixbuf-2.0-0 \
    shared-mime-info fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY --from=fonts /fonts /usr/share/fonts/truetype/ibm-plex

RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra self-host --extra slack --extra reports
COPY instructions.md ./
COPY workspace/skills ./skills
COPY config/accounts.example.toml config/write-policy.example.toml ./config/
RUN mkdir -p workspace/in workspace/out workspace/analysis workspace/logs

ENV PATH="/app/.venv/bin:${PATH}" PAID_MEDIA_API_HOST=0.0.0.0 PAID_MEDIA_API_PORT=8080
EXPOSE 8080
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"
CMD ["paid-media-agent", "serve"]

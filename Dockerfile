FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY engine.py app.py db.py notify.py run_issues.py cli.py entrypoint.py scoring_constants.py default_config.json ./
COPY templates/ templates/
# Bootstrap + the Inter webfont, served from here rather than a CDN so the UI
# loads at full speed on a host with no outbound internet.
COPY static/ static/

# In-container CLI: `mediareducer` (short alias: `mr`) drives the running
# service over its local API, e.g. `docker exec -it mediareducer mr status`.
RUN printf '#!/bin/sh\nexec python3 /app/cli.py "$@"\n' > /usr/local/bin/mediareducer \
 && cp /usr/local/bin/mediareducer /usr/local/bin/mr \
 && chmod 755 /usr/local/bin/mediareducer /usr/local/bin/mr

RUN mkdir -p /config

ENV MEDIAREDUCER_CONFIG=/config/config.json
ENV PYTHONUNBUFFERED=1

EXPOSE 7474

# The UI polls /api/status constantly, so it doubles as the health probe.
# Pure-Python probe — the slim image ships no curl/wget.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7474/api/status', timeout=8)"]

# Optional PUID/PGID user mapping (see entrypoint.py); root without them.
ENTRYPOINT ["python3", "entrypoint.py"]

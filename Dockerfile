# iSponsorBlockTV WebUI
# Builds on top of the official iSponsorBlockTV image and adds a web interface.
# The web interface starts and supervises iSponsorBlockTV inside the same container,
# so no Docker socket or host access is needed.
ARG ISBTV_IMAGE=ghcr.io/dmunozv04/isponsorblocktv:latest
FROM ${ISBTV_IMAGE}

LABEL org.opencontainers.image.title="iSponsorBlockTV WebUI" \
      org.opencontainers.image.description="Unofficial web interface for iSponsorBlockTV" \
      org.opencontainers.image.licenses="GPL-3.0-only"

COPY webui /webui

ENV WEBUI_PORT=8080 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('WEBUI_PORT','8080')+'/api/health',timeout=4)" || exit 1

WORKDIR /app
ENTRYPOINT ["python3", "-u", "/webui/server.py"]

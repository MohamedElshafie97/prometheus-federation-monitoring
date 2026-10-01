FROM python:3.12-slim

LABEL org.opencontainers.image.title="alert-dispatcher" \
      org.opencontainers.image.description="Alertmanager webhook to Google Chat"

RUN useradd --system --no-create-home --shell /usr/sbin/nologin dispatcher

WORKDIR /app
COPY alert_dispatcher.py .

USER dispatcher
EXPOSE 9095

# Config (with the webhook URLs) is mounted at runtime, never baked in
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9095/healthz', timeout=2)"

ENTRYPOINT ["python3", "-u", "alert_dispatcher.py", "--config", "/etc/alert-dispatcher/dispatcher.ini"]

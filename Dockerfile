# clv-engine: paper-only, read-only. No secrets baked in; pass env at runtime.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DB_PATH=/data/clv.sqlite ALERT_FILE=/data/alerts.jsonl REPORT_DIR=/data/reports
RUN pip install --no-cache-dir tzdata \
 && useradd --create-home --uid 10001 clv \
 && mkdir -p /data && chown clv:clv /data
WORKDIR /app
COPY clv_engine ./clv_engine
USER clv
VOLUME ["/data"]
ENTRYPOINT ["python", "-m", "clv_engine", "--env-file", "/dev/null"]
CMD ["run"]

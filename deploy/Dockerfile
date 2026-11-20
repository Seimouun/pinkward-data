# Dauer-Crawler für pinkward-data (siehe deploy/README.md). Gebaut aus dem Repo-Wurzelordner:
#   docker compose -f deploy/docker-compose.yml up -d --build
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY leaguestats ./leaguestats
COPY deploy/run.sh ./run.sh
RUN chmod +x run.sh && useradd --create-home crawler && mkdir -p /data && chown crawler /data
USER crawler

VOLUME ["/data"]
CMD ["./run.sh"]

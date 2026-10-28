FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd -r mppkb && useradd -r -g mppkb -d /app -s /sbin/nologin mppkb

COPY deploy/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY mppkb ./mppkb
COPY ontology.yaml ./ontology.yaml

RUN mkdir -p /data && chown -R mppkb:mppkb /app /data
USER mppkb

ENV MPPKB_KB_DB=/data/kb.sqlite \
    MPPKB_CORPUS_DB=/data/corpus.sqlite \
    MPPKB_CORPUS_DIR=/data/pdfs \
    MPPKB_CHROMA_DIR=/data/chroma \
    MPPKB_ONTOLOGY=/app/ontology.yaml

EXPOSE 8791

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import json,urllib.request; d=json.load(urllib.request.urlopen('http://127.0.0.1:8791/api/health',timeout=3)); raise SystemExit(0 if d.get('status')=='ok' else 1)"

CMD ["python", "-m", "mppkb.ui_bridge", "--repo", "/app/MPP_Project", "--host", "0.0.0.0", "--port", "8791"]

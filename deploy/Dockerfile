# One client instance of the panel and worker. Data (database, keys, backups)
# lives in the /dados volume, never in the image. Build from the repository
# root:  docker build -f deploy/Dockerfile -t agente .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENTE_DADOS=/dados \
    HOST_PAINEL=0.0.0.0 \
    PORTA_PAINEL=8777 \
    PROXY_HTTPS=true \
    ABRIR_NAVEGADOR=false

WORKDIR /app
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt

COPY . .
RUN useradd --create-home --uid 10001 agente \
    && mkdir -p /dados \
    && chown agente:agente /dados
USER agente
VOLUME ["/dados"]
EXPOSE 8777

# /saude answers 503 on failure; from inside the container the host is local.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8777/saude', timeout=5).status == 200 else 1)"

CMD ["python", "executar.py"]

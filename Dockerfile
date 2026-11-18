# The agent app as a container, for a deployment without Cloud Foundry
# (deploy/local: PostgreSQL, an OIDC provider and oauth2-proxy next to it).
# On BTP the app is still deployed with the MTA; this image is not used there.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 agent

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py agents.seed.json ./
COPY agents ./agents
COPY templates ./templates
COPY static ./static
COPY deploy/local/entrypoint.sh /usr/local/bin/agent-entrypoint
RUN chmod 0755 /usr/local/bin/agent-entrypoint

USER agent
ENV HOST=0.0.0.0 \
    PORT=7932
EXPOSE 7932
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=10 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7932/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["agent-entrypoint"]
CMD ["python", "app.py"]

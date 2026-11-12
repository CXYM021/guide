FROM python:3.12-slim

WORKDIR /app
COPY . /app/

ENV PYTHONUNBUFFERED=1 \
    PROF_HOST=0.0.0.0 \
    PROF_PORT=8765 \
    PROF_DATA_DIR=/app/data

RUN mkdir -p /app/data && python -m compileall -q /app/engine /app/server.py /app/auth.py /app/tenant.py

VOLUME ["/app/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8765/healthz',timeout=3); sys.exit(0 if r.status==200 else 1)"

CMD ["python", "server.py"]

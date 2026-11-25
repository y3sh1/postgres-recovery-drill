FROM postgres:17.12-trixie@sha256:87c8b42f423b45d5247e6aa8fdb2e7762fec164c68a50fe3eb07e7bfcaf4dbcf

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv \
    && install -d -o postgres -g postgres /reports

WORKDIR /app
COPY requirements*.txt ./
RUN python -m pip install --no-cache-dir -r requirements-dev.txt
COPY shop/ shop/
COPY drill/ drill/
COPY tests/ tests/

USER postgres
ENTRYPOINT []
CMD ["python", "-m", "drill", "--out", "/reports"]

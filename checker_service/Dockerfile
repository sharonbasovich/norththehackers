FROM python:3.12-slim

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 checker

RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.30" "httpx>=0.27"

USER checker
ENV PATH="/home/checker/.elan/bin:${PATH}"
RUN curl --proto '=https' --tlsv1.2 -sSf https://elan.lean-lang.org/elan-init.sh \
    | sh -s -- -y --default-toolchain none

WORKDIR /srv/lean
COPY --chown=checker:checker lean/ ./
RUN lake update && lake exe cache get && lake build

WORKDIR /srv
COPY --chown=checker:checker backend/app ./app
COPY --chown=checker:checker checker_service ./checker_service

EXPOSE 8001
CMD ["uvicorn", "checker_service.main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]

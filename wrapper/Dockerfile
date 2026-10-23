FROM python:3.11-slim

# emSign CA 키 유출(GHSA-6q48-6c7w-9gw2): OS 저장소에서 emSign ECC Root CA - G3 를 빼고,
# httpx 가 쓰는 certifi 와 pip(내장 certifi) 을 고친 버전 이상으로 둔다.
RUN apt-get update && apt-get install -y --no-install-recommends --only-upgrade ca-certificates \
    && sed -i 's|^mozilla/emSign_ECC_Root_CA_-_G3.crt$|!&|' /etc/ca-certificates.conf \
    && update-ca-certificates --fresh \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -U "pip>=26.2.2" "certifi>=2026.10.19" \
    && pip install --no-cache-dir \
    fastapi "uvicorn[standard]" pymupdf httpx python-multipart aiosqlite pyyaml

WORKDIR /app
COPY main.py figures.py version.py dashboard.html figures.html status.html metrics.html ./
COPY static ./static

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

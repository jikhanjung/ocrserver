FROM python:3.11-slim

RUN pip install --no-cache-dir \
    fastapi "uvicorn[standard]" pymupdf httpx python-multipart aiosqlite pyyaml

WORKDIR /app
COPY main.py dashboard.html status.html metrics.html ./
COPY static ./static

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

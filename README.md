# ocrserver

vLLM-based OCR server packaged as a single Docker image. Serves the [Chandra2 OCR model](https://huggingface.co/datalab-to/chandra-ocr-2) (`datalab-to/chandra-ocr-2`) behind an OpenAI-compatible HTTP API.

The same image runs on:

- **Local GPU servers** (e.g. Quadro RTX 8000, Turing CC 7.5) — FP16
- **Cloud rentals** (e.g. RunPod A40, Ampere CC 8.6) — BF16

dtype is auto-selected at container start from the GPU's compute capability. Override with `VLLM_DTYPE` if you want to force one.

---

## Quick start — local

Requires NVIDIA driver + Docker + NVIDIA Container Toolkit on the host. See [docs/INSTALL_LOCAL.md](docs/INSTALL_LOCAL.md) for the full setup walkthrough.

```bash
# Build (downloads ~10-20GB of model weights into the image)
docker build -t honestjung/ocrserver:0.1.0 -t honestjung/ocrserver:latest .

# Run on a single GPU (device 0)
docker run --rm -it --gpus '"device=0"' -p 8000:8000 honestjung/ocrserver:0.1.0
```

Two-GPU pattern (one container per card):

```bash
docker compose -f docker-compose.local.yml up -d
# chandra-a → http://localhost:8000
# chandra-b → http://localhost:8001
```

`docker-compose.local.yml` mounts `./pdfs` (input, ro) and `./ocr_json` (output, rw) into both containers. Point these at whatever your client expects.

## Quick start — RunPod

See [docs/RUNPOD.md](docs/RUNPOD.md). Short version: deploy the image as a Pod with an A40 (or similar Ampere/Ada GPU), expose port 8000.

---

## API

OpenAI chat-completions, model name `chandra` (override via `VLLM_SERVED_NAME`):

```bash
curl http://localhost:8000/health
curl http://localhost:8000/v1/models

curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "chandra",
    "messages": [{"role": "user", "content": [
      {"type": "text", "text": "Extract all text from this page in markdown."},
      {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
    ]}],
    "max_tokens": 8192
  }'
```

---

## Environment variables

All are optional. Defaults are baked into the Dockerfile; override with `-e VAR=value` (or compose `environment:`).

| Variable | Default | Notes |
|---|---|---|
| `VLLM_DTYPE` | *(auto)* | Unset → `entrypoint.sh` picks `float16` for CC < 8.0 (Turing/Volta) and `bfloat16` for CC ≥ 8.0 (Ampere+). Set explicitly to override (`float16`, `bfloat16`, `auto`). |
| `VLLM_MODEL` | `datalab-to/chandra-ocr-2` | HF model ID. Changing this defeats the baked-in weights — vLLM will re-download at runtime. |
| `VLLM_SERVED_NAME` | `chandra` | Model name clients pass in `"model": "..."`. |
| `VLLM_MAX_MODEL_LEN` | `12384` | Max sequence length. Reduce if you hit OOM. |
| `VLLM_GPU_UTIL` | `0.90` | `--gpu-memory-utilization`. Drop to `0.85` if OOM on tight VRAM. |
| `VLLM_EXTRA_ARGS` | *(empty)* | Appended to the vLLM command verbatim. Useful for `--enforce-eager`, `--quantization`, etc. |

---

## Batch OCR helper

`batch_ocr.py` ships in the image at `/workspace/batch_ocr.py`. It scans a directory of PDFs, renders each page to JPEG, hits the local vLLM endpoint, and writes one `<sha256>.json` per PDF.

```bash
# Inside a running container
docker exec -it <container> python3 /workspace/batch_ocr.py --resume

# Or on the host (after `pip install PyMuPDF Pillow requests`) against any endpoint
python3 batch_ocr.py \
  --input-dir ./pdfs \
  --output-dir ./ocr_json \
  --vllm-url http://localhost:8000 \
  --concurrency 4 --resume
```

Key flags: `--dpi 150`, `--concurrency 4`, `--resume` (skip already-processed files), `--limit N` (cap files per run).

Output JSON shape:

```json
{
  "pdf": "paper.pdf",
  "hash": "<sha256>",
  "total_pages": 12,
  "done_pages": 12,
  "failed_pages": 0,
  "failed_page_numbers": [],
  "pages": [{"page": 0, "markdown": "...", "duration_ms": 1234}, ...]
}
```

---

## Repository layout

```
ocrserver/
├── Dockerfile               # vLLM base + model bake + entrypoint
├── entrypoint.sh            # CC-aware dtype dispatcher
├── batch_ocr.py             # Batch driver (also usable standalone)
├── docker-compose.local.yml # 2-GPU local pattern
└── docs/
    ├── INSTALL_LOCAL.md     # Host setup (driver, Docker, toolkit)
    └── RUNPOD.md            # RunPod deployment notes
```

---

## Notes

- Model weights are baked into the image at build time → fast cold start, larger image (~15–25GB).
- vLLM falls back gracefully on Turing where FlashAttention 2 paths aren't supported; expect 20–40% lower throughput vs Ampere on the same VRAM budget.
- Tensor parallel is **not** used — for multi-GPU, run one container per card (see `docker-compose.local.yml`).

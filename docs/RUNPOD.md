# RunPod deployment

Notes for running the `ocrserver` image on [RunPod](https://www.runpod.io/).

## Pod vs Serverless

| | Reserved Pod | Serverless |
|---|---|---|
| Pricing | per-hour while running | per-request (cold start each call) |
| Cold start | seconds (model is baked) | tens of seconds + queue wait |
| Best for | sustained batch jobs (thousands of pages) | bursty / low-volume traffic |
| This repo targets | ✅ | ❌ (no serverless adapter included) |

For sustained batch workloads, a reserved Pod is dramatically cheaper than serverless. The trade-off is **you pay for the entire wall-clock time the Pod is up, including idle** — *always stop the Pod when the batch is done.*

## Deploy

1. Build & push the image to a registry RunPod can pull from (Docker Hub, GHCR, or build directly on RunPod).
   ```bash
   docker build -t honestjung/ocrserver:latest .
   docker push honestjung/ocrserver:latest
   ```
2. RunPod console → **Pods → Deploy**.
3. **GPU**: A40 (48GB) is a good default for this model. Any Ampere or newer card with ≥40GB VRAM works; bf16 is selected automatically.
4. **Container Image**: `honestjung/ocrserver:latest`.
5. **Expose HTTP Port**: `8000`.
6. **Volume**: not required — model weights are inside the image. Attach one only if you want OCR output to survive Pod termination.
7. Deploy.

## Verify

Once the Pod boots (1–3 minutes after the image is pulled and cached):

```bash
curl http://<pod-ip>:8000/health
curl http://<pod-ip>:8000/v1/models       # → "chandra"
```

The container log should show `[entrypoint] GPU CC=86 -> VLLM_DTYPE=bfloat16` (A40 is CC 8.6).

## Cost ballpark

- A40 reserved: ~$0.39/hr (RunPod community cloud, varies)
- Reference: a 300k-page batch typically lands in the **$8–$25** range total — roughly **40–100× cheaper** than the equivalent on serverless GPU pricing.

> ⚠️ **Stop the Pod when finished.** Idle reserved Pods bill at the same hourly rate as active ones. A forgotten Pod over a weekend can erase the entire cost advantage.

## Tuning knobs

See the env-var table in [../README.md](../README.md). Common adjustments on RunPod:

- `VLLM_GPU_UTIL=0.85` — drop a notch if you see OOM on smaller cards.
- `VLLM_MAX_MODEL_LEN=8192` — reduce if you don't need full-length context.
- `VLLM_DTYPE=bfloat16` — usually auto, but set explicitly if the auto-detect logs `dtype=auto`.

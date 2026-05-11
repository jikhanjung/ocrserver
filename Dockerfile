FROM vllm/vllm-openai:latest

# Bake model weights into the image so the container starts fast and works offline.
RUN python3 -c "from huggingface_hub import snapshot_download; snapshot_download('datalab-to/chandra-ocr-2')"

# Deps for batch_ocr.py (PDF rendering + image processing).
RUN pip3 install --no-cache-dir PyMuPDF Pillow

COPY entrypoint.sh /workspace/entrypoint.sh
COPY batch_ocr.py /workspace/batch_ocr.py
RUN chmod +x /workspace/entrypoint.sh && mkdir -p /workspace/pdfs /workspace/ocr_json

# Defaults; override at `docker run -e ...` time.
# VLLM_DTYPE intentionally unset — entrypoint.sh picks fp16/bf16 from GPU compute capability.
ENV VLLM_MODEL=datalab-to/chandra-ocr-2 \
    VLLM_SERVED_NAME=chandra \
    VLLM_MAX_MODEL_LEN=12384 \
    VLLM_GPU_UTIL=0.90

EXPOSE 8000

# Run vLLM. Run batch_ocr.py separately via `docker exec`:
#   docker exec -it <container> python3 /workspace/batch_ocr.py --resume
ENTRYPOINT ["/workspace/entrypoint.sh"]

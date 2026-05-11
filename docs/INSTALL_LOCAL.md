# Local Server 설치 가이드 (ocrserver)

로컬 GPU 서버에 NVIDIA 드라이버 + Docker + NVIDIA Container Toolkit을 설치해 `chandra-vllm` 이미지를 돌리기 위한 절차.

## 대상 환경 (이 가이드가 가정하는 것)

- Ubuntu 26.04 LTS (resolute), kernel 7.x
- NVIDIA Quadro RTX 8000 × 2 (각 48GB VRAM, Turing / CC 7.5)
- Secure Boot **disabled** (`mokutil --sb-state`로 확인)
- 인터넷 연결, sudo 권한
- 디스크 free ≥ 60GB (이미지 + 모델 + OCR 출력 캐시 여유)

> Secure Boot가 켜져 있으면 NVIDIA 커널 모듈에 MOK 등록 절차가 추가로 필요합니다. 이 가이드는 disabled 전제.

---

## 0. 사전 점검

```bash
# OS / kernel
lsb_release -a
uname -r

# GPU 인식
lspci | grep -iE 'vga|3d'

# 추천 드라이버 확인 (resolute 기준: nvidia-driver-595-open 권장)
ubuntu-drivers devices

# 현재 nouveau만 로드 중인지
lsmod | grep -E 'nvidia|nouveau'

# 디스크 / 메모리
df -h /
free -h
```

기대: `lspci`에 RTX 8000 두 장, `lsmod`에 `nouveau`만, `nvidia` 모듈은 없는 상태.

---

## 1. NVIDIA proprietary 드라이버 설치

### 1-1. 설치

```bash
# 추천 드라이버 자동 선택 (resolute에서는 nvidia-driver-595-open)
sudo ubuntu-drivers install

# 또는 명시적으로
sudo apt update
sudo apt install -y nvidia-driver-595
```

`-open`/non-open 차이: Turing 이상은 `-open` (오픈 커널 모듈)도 안정적입니다. `ubuntu-drivers`가 권장하는 것을 그대로 사용.

### 1-2. nouveau blacklist 확인

`nvidia-driver-*` 패키지가 자동으로 `/etc/modprobe.d/nvidia.conf`에 nouveau blacklist를 깔지만, 명시적으로 확인:

```bash
cat /etc/modprobe.d/blacklist-nouveau.conf 2>/dev/null || cat /etc/modprobe.d/nvidia.conf 2>/dev/null
```

`blacklist nouveau` 라인이 없다면 추가:

```bash
sudo tee /etc/modprobe.d/blacklist-nouveau.conf > /dev/null <<'EOF'
blacklist nouveau
options nouveau modeset=0
EOF
sudo update-initramfs -u
```

### 1-3. 재부팅 후 검증

```bash
sudo reboot
```

부팅 후:

```bash
nvidia-smi
```

기대 출력 — 두 카드가 `Quadro RTX 8000`, 각각 `48G` 정도로 표시:

```
+-----------------------------------------------------------------------------+
| NVIDIA-SMI 595.xx     Driver Version: 595.xx     CUDA Version: 12.x         |
|---------------------------------+----------------------+----------------------+
| GPU  Name              Persistence-M| Bus-Id      Disp.A | Volatile Uncorr. ECC |
|=================================+======================+======================|
|   0  Quadro RTX 8000          On  | 00000000:17:00.0 Off |                  Off |
|   1  Quadro RTX 8000          On  | 00000000:65:00.0 Off |                  Off |
+-----------------------------------------------------------------------------+
```

`nvidia-smi` 실패 시 자주 보이는 원인:
- nouveau가 여전히 로드 중 → `lsmod | grep nouveau` 확인, blacklist 누락
- DKMS 빌드 실패 → `sudo dmesg | grep -i nvidia` 또는 `journalctl -k | grep -i nvidia`로 로그 확인
- Secure Boot가 실제로는 켜져 있어서 모듈 서명 거부 → `mokutil --sb-state` 재확인

---

## 2. Docker Engine 설치

Ubuntu 26.04(resolute)는 신규 코드네임이라 Docker 공식 apt 저장소에 아직 channel이 없을 수 있습니다. 두 옵션 중 택일:

### 옵션 A: Ubuntu repo의 `docker.io` (간단, 권장)

```bash
sudo apt update
sudo apt install -y docker.io docker-buildx
sudo systemctl enable --now docker
```

### 옵션 B: Docker 공식 저장소 (resolute 채널이 열린 뒤)

```bash
# 채널 존재 확인
curl -sSL https://download.docker.com/linux/ubuntu/dists/ | grep -i resolute

# 존재할 경우에만 진행 — 아니면 옵션 A로
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu resolute stable" | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

### 2-1. 사용자 그룹

```bash
sudo usermod -aG docker $USER
# 새 세션에서 적용 (로그아웃/로그인 또는):
newgrp docker

docker run --rm hello-world
```

---

## 3. NVIDIA Container Toolkit

`docker run --gpus all` 동작에 필요. 공식 저장소의 안정 채널을 사용.

```bash
# 키 + repo
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt update
sudo apt install -y nvidia-container-toolkit

# Docker runtime에 nvidia 등록
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### 3-1. 검증

```bash
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

컨테이너 안에서 호스트와 동일하게 RTX 8000 두 장이 보이면 통과. 자주 보이는 실패:

- `could not select device driver "" with capabilities: [[gpu]]` → toolkit 설치는 됐지만 `runtime configure`를 안 했거나 Docker 재시작을 안 함
- `Failed to initialize NVML: Driver/library version mismatch` → 드라이버 설치 후 재부팅 안 했음

---

## 4. ocrserver 이미지 빌드 + 첫 실행

> Turing(RTX 8000 등 CC 7.5)은 BF16 미지원이지만, `entrypoint.sh`가 GPU compute capability를 감지해 자동으로 `--dtype float16`을 선택합니다. 별도 수정 불필요.

빌드 (이 repo 루트에서):

```bash
cd ~/projects/ocrserver
docker build -t chandra-vllm:latest .
```

- 빌드 중 `datalab-to/chandra-ocr-2`를 HuggingFace에서 받아 이미지에 굽기 때문에 한 번에 10-20GB 다운로드/저장. 디스크 free를 미리 확인할 것.
- 인터넷 끊김에 대비해 `tmux` / `screen` 안에서 실행 추천.

단일 카드로 시험 실행 (GPU 0번만, 입출력은 현재 디렉토리에 마운트):

```bash
mkdir -p pdfs ocr_json
docker run --rm -it \
  --gpus '"device=0"' \
  -p 8000:8000 \
  -v "$PWD/pdfs":/workspace/pdfs:ro \
  -v "$PWD/ocr_json":/workspace/ocr_json \
  chandra-vllm:latest
```

다른 터미널에서:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/v1/models
```

기동 로그에 `[entrypoint] GPU CC=75 -> VLLM_DTYPE=float16` 이 보이면 정상 (RTX 8000 기준).

배치 OCR (vLLM이 ready된 뒤 별도 셸):

```bash
docker exec -it <container-id> python3 /workspace/batch_ocr.py --resume
```

---

## 5. 2장 동시 운용 패턴

RTX 8000 × 2 환경이므로 컨테이너를 2개 띄워 각 GPU에 1장씩 핀하는 방식이 가장 단순합니다 (vLLM tensor parallel 안 씀). repo 루트의 `docker-compose.local.yml`이 이 패턴을 그대로 코드화한 파일:

```bash
cd ~/projects/ocrserver
mkdir -p pdfs ocr_json   # compose가 마운트하는 호스트 디렉토리
docker compose -f docker-compose.local.yml up -d

docker compose -f docker-compose.local.yml ps
docker compose -f docker-compose.local.yml logs -f chandra-a
```

기동 후 endpoint는:

- chandra-a (GPU 0) → `http://localhost:8000`
- chandra-b (GPU 1) → `http://localhost:8001`

수동으로 띄우고 싶다면:

```bash
docker run -d --name chandra-a --gpus '"device=0"' -p 8000:8000 \
  -v "$PWD/pdfs":/workspace/pdfs:ro \
  -v "$PWD/ocr_json":/workspace/ocr_json chandra-vllm:latest

docker run -d --name chandra-b --gpus '"device=1"' -p 8001:8000 \
  -v "$PWD/pdfs":/workspace/pdfs:ro \
  -v "$PWD/ocr_json":/workspace/ocr_json chandra-vllm:latest
```

클라이언트 쪽에서는 endpoint URL 두 개를 round-robin 또는 idle worker 큐로 분배하면 됩니다.

---

## 6. 트러블슈팅 빠른 표

| 증상 | 원인 / 확인 |
|------|-------------|
| `nvidia-smi` not found 또는 module not loaded | 재부팅 안 함 / nouveau가 여전히 로드 / `sudo dmesg \| grep nvidia` |
| `docker: Cannot connect to the Docker daemon` | `sudo systemctl status docker`, 사용자 그룹 미반영 (`newgrp docker`) |
| `could not select device driver "" with capabilities: [[gpu]]` | `nvidia-ctk runtime configure --runtime=docker` 미실행 또는 docker 재시작 안 함 |
| vLLM 시작 시 `RuntimeError: bfloat16 is not supported on this GPU` | `entrypoint.sh`의 CC 감지가 실패했거나 `VLLM_DTYPE=bfloat16`이 명시 설정됨 → 로그의 `[entrypoint] GPU CC=...` 확인, 필요 시 `-e VLLM_DTYPE=float16`으로 강제 |
| `CUDA out of memory` | `--gpu-memory-utilization`을 0.85로 낮추거나 `--max-model-len` 축소 |
| 배치 처리가 RunPod A40 대비 느림 | Turing은 FlashAttention 2 일부 최적화 미지원 — 정상. throughput 20-40% 하락 가능 |

---

## 7. 운영 메모

- `docker system df` / `docker image prune`로 빌드 캐시 주기적 정리. 모델 레이어가 무거워 잘못 받으면 디스크 빠르게 참
- `nvidia-smi -l 2`로 실시간 GPU 사용량 / 온도 / power 확인 (RTX 8000 TDP 260W × 2)
- 장시간 배치는 `tmux` + `docker logs -f` 조합으로 안정적으로 추적
- 모델 파일이 이미지에 구워져 있으므로 컨테이너 시작 시간은 빠름 (drive cache에 따라 1-3분)

---

## 다음 단계

1. 이 가이드대로 드라이버 + Docker + Toolkit 설치 후 `nvidia-smi`(host) → `docker run --gpus all` 검증까지 통과
2. `docker build -t chandra-vllm:latest .` → 기동 로그에서 `[entrypoint] GPU CC=75 -> VLLM_DTYPE=float16` 확인
3. `curl http://localhost:8000/health` 및 `/v1/models` 응답 확인
4. `batch_ocr.py`로 1페이지 PDF 한 건 처리해 OCR 품질 spot-check (Turing FP16에서 정확도 손실이 없는지)
5. `docker compose -f docker-compose.local.yml up -d`로 2-GPU 동시 기동, `nvidia-smi -l 2`로 두 카드 점유 확인

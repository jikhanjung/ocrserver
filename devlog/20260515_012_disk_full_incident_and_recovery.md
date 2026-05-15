# 20260515_012 — 인시던트: 루트 디스크 풀 + 디스크 확장/HDD 마운트

## 증상

낮 시간에 루트 파티션이 99% (92 G / 98 G) 까지 차서 응급으로 `/srv/ocrserver/data/pdfs/` 의 일부 PDF (hash `a*`, `b*` 시작) 를 수동 삭제해 공간을 확보했음. 그 와중에도 OCR 처리는 계속 돌고 있는 것처럼 보였음.

```
/dev/mapper/ubuntu--vg-ubuntu--lv   98G   92G  1.6G  99% /
```

## 원인

루트 LV 가 100 GiB 로 만들어져 있었고, 정작 nvme PV (`/dev/nvme0n1p3`, ~474 GiB) 의 대부분이 미할당 상태였음. 큰 점유 요소는:

- `/var/lib/docker` containerd snapshot (~60 GB, chandra + vLLM 이미지)
- `/srv/ocrserver/hf_cache/models--Qwen--Qwen3-14B` = **28 GB**
- `/srv/ocrserver/data/pdfs/<sha256>.pdf` 누적

PDF 한 건 누적이 root 풀의 직접 원인은 아니지만, LV 슬랙이 너무 적어 작은 누적도 위험 임계로 끌어올렸음.

## 왜 OCR 은 계속 돌았나

`wrapper/main.py` 핫패스는 디스크에 거의 의존하지 않음.

- `/ocr` POST 핸들러에서 `pdf_bytes = await file.read()` 로 전체 바이트를 메모리에 읽고, `/data/pdfs/<hash>.pdf` 는 **캐시/아카이브 용도로 한 번 저장**될 뿐.
- `background_tasks.add_task(_run, job_id, pdf_bytes)` 로 처리 함수에 **bytes 를 직접 전달**.
- `_run` 안에서 `fitz.open(stream=pdf_bytes, …)` — 디스크가 아닌 메모리에서 PDF 열고, 페이지를 JPEG 로 렌더링해서 base64 로 chandra HTTP API 에 전송.
- chandra 의 `./pdfs:/workspace/pdfs:ro` 마운트는 실제로는 안 쓰임 (페이지 이미지는 HTTP 본문으로 전달).
- chandra 자체는 모델이 GPU 메모리에 상주 + HTTP I/O 만 함. 호스트 디스크 쓰기는 stdout 로그 정도.

즉 `/data/pdfs/a*.pdf`, `b*.pdf` 를 지워도 **이미 메모리에 올라간 job 들은 끝까지 처리됨**.

## 위험했던 경로 (이번엔 안 터진 것들)

- **새 업로드**: `with open(pdf_path, "wb") as f: f.write(pdf_bytes)` (`main.py:312`) — 디스크 풀이면 `OSError` 로 `/ocr` POST 가 500. 이미 진행 중인 job 엔 무관.
- **sqlite 쓰기 실패**: `"database or disk is full"` 에러 가능. 페이지 row 가 `ok` 로 기록되지 않으면 `_jobs` 메모리 dict 에만 남고, wrapper 재시작 시 `_resume_processing_jobs` 가 그 페이지를 미완료로 보고 재처리.
- **`_resume_processing_jobs` (`main.py:415-437`)**: 디스크의 `/data/pdfs/<hash>.pdf` 를 다시 열어 페이지를 재렌더링하는 구조. 파일이 없으면 `resume failed: missing <path>` 로 status=failed.
  - 만약 PDF 삭제 시기에 wrapper / docker daemon 재시작이 겹쳤다면 (`unattended-upgrades` 자동 재시작 사례는 [007]) 진행 중이던 `a*`, `b*` 해시 job 의 남은 페이지가 모두 실패 처리됐을 것. **이번엔 운 좋게 재시작이 안 겹쳤음**.
- **docker overlay upper layer**: chandra / vLLM 이 임시 파일 / CUDA 캐시 등을 쓰는 순간 풀 디스크면 추론 자체가 죽을 수 있음.

## 무결성 확인

디스크 풀 시기에 sqlite 쓰기 실패가 조용히 누적되지 않았는지 두 가지 체크:

```sql
-- (1) jobs 카운터 자체가 모자란 done/done_with_errors
SELECT job_id, total_pages, done_pages, failed_pages
FROM jobs
WHERE status IN ('done','done_with_errors')
  AND total_pages > 0
  AND (done_pages + failed_pages) < total_pages;

-- (2) pages 테이블의 ok/failed row 수가 total_pages 보다 적은 done
SELECT j.job_id,
       (SELECT COUNT(*) FROM pages p WHERE p.job_id=j.job_id) AS any_rows,
       j.total_pages
FROM jobs j
WHERE j.status IN ('done','done_with_errors')
  AND j.total_pages > 0
  AND (SELECT COUNT(*) FROM pages p WHERE p.job_id=j.job_id) < j.total_pages;
```

두 쿼리 모두 **0 건**. 페이지 결과 누락 없이 데이터 무결성이 유지됐음.

## 복구 조치 (오늘 적용)

### 1. 루트 LV 확장 (무중단)

nvme PV 에 남아있던 ~374 GiB 미할당 공간 중 250 GiB 를 LV 에 추가.

```
sudo lvextend -L +250G /dev/ubuntu-vg/ubuntu-lv
sudo resize2fs /dev/mapper/ubuntu--vg-ubuntu--lv
```

결과: root `98G → 344G` (Used 92 G, Avail **238 G**). ext4 online resize 이므로 컨테이너 중단 없음. nvme PV 에 여전히 ~124 GiB 미할당이 남아있어 추후 추가 확장 여지 있음.

### 2. 4 TB HDD 마운트 (`/dev/sda` → `/mnt/disk1`)

물리적으로는 이미 연결되어 있었으나 fstab 에 없고 마운트되지 않은 채 방치돼 있던 Toshiba HDWE140. ext4 + label `disk1`. UUID 로 fstab 등록 후 마운트:

```
UUID=1b6444de-6ba9-42e8-b2f1-c29323304936 /mnt/disk1 ext4 defaults,nofail,x-systemd.device-timeout=10 0 2
```

- `nofail,x-systemd.device-timeout=10` — HDD 가 부팅 시 늦게 올라와도 부팅이 실패하지 않도록.

⚠️ **디스크가 빈 줄 알았는데 1.5 TB 의 예전 ML 데이터가 들어있었음** — `CheXpert-v1.0.zip` (~439 GB), `CheXpert-v1.0/` (압축 해제본), `CheXpert-v1.0-small.zip`, `NIH/`, `StyleGAN/`, `Keras-GAN/`, `llama/`, `backup_ws/`. 보존 결정. 여유는 **~2 TB**.

ocrserver 데이터를 옮긴다면 `/mnt/disk1/ocrserver/` 아래에. 다만 HDD 라 성능 민감한 것 (hf_cache, docker images, sqlite DB) 은 NVMe 유지가 원칙. 이번엔 루트 LV 확장만으로 충분해서 실제 데이터 이동은 보류.

## 교훈

- 운영 디스크는 **사용량이 늘어나는 디렉토리에 슬랙을 충분히** 두자. LVM 확장이 무중단으로 가능하다는 걸 알고 있으면 응급 처치도 쉬워짐.
- wrapper 의 in-memory 파이프라인 + 디스크는 캐시 라는 설계가 이번 인시던트에서 결정적인 buffer 역할을 함. 의도된 설계는 아니었지만 운영 안정성에 큰 기여.
- 다만 `_resume_processing_jobs` 는 디스크 PDF 에 의존하므로, **장애 시점에 wrapper / docker daemon 재시작이 겹치면 데이터 손실 위험**이 실재함. 디스크 풀 같은 상황에서는 PDF 삭제와 컨테이너 재시작을 절대 같이 하지 말 것.
- 4 TB HDD 가 비어있는 줄 알았던 건 lsblk 출력만 보고 짐작한 결과. 마운트하기 전까지 내용 확인이 안 됐을 뿐, **연결만 안 됐지 빈 디스크가 아니었음**. 다음에 비슷한 작업 할 때 이 점 주의.

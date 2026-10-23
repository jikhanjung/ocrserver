# 054 — 루트 디스크 92% → 76%: Qwen3-14B 삭제, hf_cache·figure_ws 를 HDD 로 (2026-10-23)

## 왜

루트(NVMe LVM, 466 GB)가 10-22 10:08 81%(88 GB 여유)에서 10-23 03:15 **92%(37 GB)** 가 됐다.
볼륨 그룹 여유가 0 이라(`sudo vgs` VFree 0, 사용자 확인) `lvextend` 로는 늘릴 수 없다.

## 늘어난 51 GB 의 정체 (추정)

- Docker 는 containerd 이미지 저장소(`driver-type io.containerd.snapshotter.v1`)를 쓴다.
  사용자 sudo 로 잰 크기: `io.containerd.content.v1.content` 25 GB, `io.containerd.snapshotter.v1.overlayfs` 113 GB. 이미지 실체는 약 88 GB 다.
- 오늘 chandra 0.1.2(52 GB)·vllm 0.20.2-p1(32 GB)을 빌드하고 push 하면서 압축 레이어(content)와 빌드 사본이 생긴 것으로 본다.
  지금 이미지가 참조하고 있어 `docker builder prune` 으로는 194.5 MB 만 비었다. 이전 크기 기록이 없어 정확한 내역은 모른다.
- 옛 태그 정리(`ocrwrapper` 0.2.6–0.3.15·latest, `ocrserver:latest`)와 태그 없는 이미지(옛 nginx) 정리로 약 1 GB 를 비웠다.

## 한 일

1. **Qwen3-14B 삭제**(`/srv/ocrserver/hf_cache/hub/models--Qwen--Qwen3-14B`, 28 GB, 05-13 받음). 지금 LLM 은 Qwen3-32B-AWQ 다.
   root 소유라 일회용 컨테이너(`docker run --rm -v ... alpine rm -rf`)로 지웠다. → 86%.
2. **hf_cache → `/mnt/disk1/ocrserver/hf_cache`**(19 GB, Qwen3-32B-AWQ):
   - 일회용 컨테이너로 `cp -a` 하고, 25개 파일을 `cmp` 로 대조해 모두 같음을 확인했다.
   - compose 의 llm 볼륨을 절대 경로로 바꾸고, NVMe 쪽 원본을 지웠다. → 82%.
   - 영향: LLM 기동 때 모델을 HDD 에서 읽어 1–2분 느려진다(사용자 감수).
3. **figure_ws → `/mnt/disk1/ocrserver/figure_ws`**(25 GB, 209,411 파일, 도판 워커 작업 공간):
   - 경로가 systemd 유닛(`FIGURES_WS_DIR`, root 소유)에 박혀 있어, 유닛은 그대로 두고 `/srv/ocrserver/figure_ws` 를 **심볼릭 링크**로 바꿨다.
   - `max_workers` 0 은 "무제한"이고 외부 일시정지 API 가 없다. 그래서 큐가 비고 워커가 모두 idle 인 것을 확인하고 진행했다:
     rsync → `mv figure_ws figure_ws.old && ln -s` → 최종 rsync → `diff -rq` 차이 없음, 파일 수 같음 → 옛 폴더 삭제.
   - 워커 10개 alive·idle, 링크로 쓰기 확인, 오류 없음. → **76%(109 GB 여유)**. `/mnt/disk1` 56%.

## 주의

- `/mnt/disk1` 은 fstab `nofail` 이다. 부팅 때 HDD 가 안 붙으면:
  - llm 을 띄울 때 docker 가 루트에 빈 `/mnt/disk1/ocrserver/hf_cache` 를 만들고 모델 19 GB 를 다시 받는다.
  - 도판 워커는 링크가 가리키는 곳에 쓸 수 없어 항목이 실패한다.
  - HDD 가 붙어 있는지(`mountpoint /mnt/disk1`)부터 본다.
- 하드디스크는 `sda` 하나뿐이다. 그 밖에는 NAS NFS(`/nfs/temp-share`, 20 TB)가 있지만 네트워크라 이런 용도에는 쓰지 않는다.

# devlog 034 — NVLink 미인식 진단: GPU1 서브링크 학습 실패

날짜: 2026-06-24
태그: 진단만 (코드/배포 변경 없음)

## 배경

033 에서 LLM 모델 후보를 보던 중, 27B 를 TP=2 로 돌리는 옵션을 검토하다가
`nvidia-smi topo -m` 이 GPU0↔GPU1 을 **`NODE`(PCIe)** 로 보고하는 걸 발견.
NVLink 브리지가 물리적으로 꽂혀 있다는데 드라이버가 NVLink 경로를 안 잡음.
TP=2 성능(레이어마다 all-reduce)이 NVLink 유무에 직결되므로 원인 추적.

## 증상

```
$ nvidia-smi topo -m
        GPU0   GPU1
GPU0     X     NODE      ← NVLink 면 NV1/NV2... 로 떠야 함
GPU1    NODE    X

$ nvidia-smi nvlink --status -i 0
NVML: Unable to retrieve Nvlink information as all links are inActive

$ nvidia-smi nvlink --capabilities -i 0
(빈 응답)
```

## 근본 원인 (dmesg)

```
$ sudo dmesg | grep -i nvlink
[ 3.41] nvidia-nvlink: Nvlink Core is being initialized, major device number 235
[ 6.08] NVRM: GPU1 knvlinkCoreSetTxSublinkModeCallback: Error setting TX sublink mode. mode = 0x00000008
[ 6.12] NVRM: GPU1 knvlinkCoreSetTxSublinkModeCallback: Error setting TX sublink mode. mode = 0x00000008
[ 6.52] NVRM: GPU1 knvlinkCoreSetRxSublinkModeCallback: Error setting RX sublink mode!
[ 6.92] NVRM: GPU1 knvlinkCoreSetRxSublinkModeCallback: Error setting RX sublink mode!
[10.92] NVRM: GPU1 knvlinkCoreSetTxSublinkModeCallback: Error setting TX sublink mode. mode = 0x0000000a
```

해석:
- NVLink **코어는 초기화됨** → 드라이버는 브리지 존재를 인지하고 링크를 올리려
  시도함.
- 그러나 **서브링크(레인) 학습이 GPU1 쪽에서 TX·RX 모두 실패** → 링크가 UP
  상태로 못 올라옴. 그래서 `topo -m` 이 PCIe(NODE) 로 폴백.
- 에러가 **GPU1 에만 집중**(GPU0 깨끗) → GPU1 쪽 NVLink 접점/브리지 컨택
  문제일 가능성이 높음. 전형적인 **브리지 안착 불량 / 접촉 불량** 패턴.
- 소프트웨어(persistence mode, 드라이버 옵션) 로는 해결 불가 — 물리 링크
  트레이닝 실패라 SW 영역 밖.

## 조치 (물리 작업, 미수행 — 다음 케이스 오픈 때)

우선순위:
1. **NVLink 브리지 재안착** — 전원 off, 양쪽 GPU 에 확실히 재장착(GPU1 쪽
   특히). 가장 흔한 해결.
2. **브리지 규격 확인** — Quadro RTX 8000 은 슬롯 간격 맞는 전용 브리지
   (2-slot/3-slot) 필요. 간격 불일치 시 접점이 떠서 정확히 이 증상.
3. 재안착 후에도 GPU1 만 실패하면 → 브리지 불량 또는 GPU1 NVLink 커넥터 핀
   문제(휨/오염) 의심. 브리지 교체 테스트.

## 재안착 후 검증 명령

```bash
nvidia-smi topo -m          # GPU0↔GPU1 이 NV1/NV2... 로 떠야 함
nvidia-smi nvlink -s        # Link N: <speed> GB/s, Active
nvidia-smi nvlink -e        # 에러 카운터 0
sudo dmesg | grep -i nvlink # 부팅 시 sublink Error 사라져야
```

## 우선순위 / 영향

- **현재 운영 무영향**: 배포된 Qwen3-32B-AWQ 는 GPU 1장에 들어가므로 NVLink
  불필요. 듀얼 분할(GPU0=OCR, GPU1=LLM) 도 NVLink 안 씀.
- NVLink 는 **나중에 TP=2 로 더 큰 모델**(27B / 70B급) 돌릴 때만 의미. 그때
  PCIe 폴백이면 통신 오버헤드로 느려짐.
- 따라서 **급하지 않음** — 다음 물리 점검 때 브리지 재안착 권장.

## 참고
- 선행: devlog 033 (LLM 모델 업그레이드), HANDOFF 2026-06-24 미해결 항목

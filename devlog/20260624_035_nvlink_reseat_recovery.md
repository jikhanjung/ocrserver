# devlog 035 — NVLink 복구 확인: 브리지 재안착으로 해결

날짜: 2026-06-24
태그: 진단/물리 작업 (코드/배포 변경 없음)

## 배경

devlog 034 에서 GPU1 NVLink 서브링크 학습 실패(TX·RX 모두)로 `topo -m` 이
NVLink 대신 PCIe(NODE) 로 폴백하던 문제를 진단. 1순위 조치로 **브리지
재안착**을 권고했었음.

## 조치 (수행)

**전원 off → NVLink 브리지 재장착 → 전원 on.**

devlog 034 의 1순위 권고 그대로. 소프트웨어 변경 없음.

## 검증 결과 — 전부 정상

```
$ nvidia-smi nvlink --status
GPU 0: Quadro RTX 8000
	 Link 0: 25.781 GB/s
	 Link 1: 25.781 GB/s
GPU 1: Quadro RTX 8000
	 Link 0: 25.781 GB/s
	 Link 1: 25.781 GB/s

$ nvidia-smi topo -m
        GPU0   GPU1
GPU0     X     NV2       ← 034 의 NODE(PCIe) → NV2(bonded 2×NVLink) 로 복구
GPU1    NV2    X

$ nvidia-smi nvlink -e
GPU 0/1, Link 0/1 전부:
	 Replay Errors: 0
	 Recovery Errors: 0
	 CRC Errors: 0

$ sudo dmesg | grep -i nvlink
[3.73] nvidia-nvlink: Nvlink Core is being initialized, major device number 236
   ← 034 의 knvlinkCoreSet{Tx,Rx}SublinkModeCallback Error 라인들 전부 사라짐
```

해석:
- GPU0/1 양쪽 각 2서브링크 모두 UP, 25.781 GB/s.
- `topo -m` 이 **NV2**(bonded 2×NVLink) — 034 의 NODE 폴백 해소.
- 에러 카운터 0, dmesg 에 sublink Error 없음 → 물리 링크 학습 정상.
- 진단대로 **브리지 안착/접촉 불량**이 근본 원인이었음이 확정.

## 영향

- 현재 운영은 NVLink 없이도 무영향이었음(032 듀얼 분할, 033 단일 카드 LLM
  모두 NVLink 미사용). 이번 복구는 **나중 TP=2 큰 모델** 대비 인프라 정상화.
- 이제 TP=2(27B / 70B급) 검토 시 PCIe 폴백 페널티 없이 NVLink all-reduce 가능.

## 교훈

- GPU1 한쪽에만 집중된 sublink Error → 전형적 브리지 접촉 불량. SW 손대지
  말고 바로 재안착이 정답이었음.
- 재발 시 비교 기준: 정상값은 `topo -m` = NV2, `nvlink -e` 전부 0.

## 참고
- 선행: devlog 034 (NVLink 미인식 진단), 033 (LLM 모델 업그레이드)

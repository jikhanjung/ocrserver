# 050 — wrapper 0.3.16 패키지 고정, 0.3.17 PyMuPDF → pypdfium2, 워커 0.3.12 (2026-10-23)

## 0.3.16 — 패키지 고정

0.3.15(devlog 049)는 Dockerfile 에 버전 고정이 없어 재빌드 때 그날의 최신 버전을 받았다.
**PyMuPDF 1.28.2 → 2.0**(onnxruntime·numpy·pymupdf4llm 동반, 이미지 357 → 689 MB), fastapi 0.143 → 0.161, starlette 1.8 → 1.9.
배포 뒤 패키지 목록을 비교하다 발견했다. 그 사이 도판 큐가 비어 있고 chandra 가 정지 중이라 PyMuPDF 2.0 으로 처리된 작업은 없다.

- `wrapper/requirements.lock`: 0.3.14 이미지의 `pip freeze` 그대로 두고 certifi 만 2026.10.19 로 올렸다. Dockerfile 은 이 파일로 설치한다.
- 0.3.16 은 0.3.14 와 패키지 목록이 certifi·pip 만 다르다. 배포하고 Hub 에 올렸다(digest `4bf6ab40…`).

## 0.3.17 — pypdfium2 (사용자 요청)

PyMuPDF 는 AGPL-3.0 이다. 이 저장소는 공개이고 이미지는 Docker Hub 로 배포한다.
PaperMeister 는 2026-08-13 에 이미 pypdfium2(BSD-3/Apache-2.0)로 옮겼고, 같은 패턴 `page.render(scale=dpi/72).to_pil().convert("RGB")` 을 쓴다.

- `wrapper/main.py`:
  - `_pdf_page_count`·`_render_one_page` 를 pdfium 으로 바꿨다.
  - JPEG 는 Pillow 로 quality 95 로 인코딩한다(PyMuPDF `tobytes("jpeg")` 기본값과 같음).
  - 잠금은 `_mupdf_lock` → `_pdf_lock` 으로 이름만 바꾸고 유지한다. pdfium 도 스레드 세이프가 아니고, 문서가 달라도 동시에 부르면 안 된다.
  - 업로드 때 페이지 수 세기(`_count_pages`)는 전에는 잠금 밖이었는데, 이제 `_pdf_page_count` 를 거쳐 잠금 안에서 한다.
- lock 파일: `pymupdf` 를 빼고 `pypdfium2==5.14.0`(10-04), `pillow==12.2.0` 을 넣었다. 출시 2주 넘은 것으로 골랐고, Pillow 13.0 은 10-16 출시라 뺐다.
- `scripts/figures_worker.py` 0.3.12:
  - `open_pdf()`·`render()` 도우미를 두고, 페이지 렌더·target·crop·페이지 수를 pdfium 으로 바꿨다.
  - crop 은 pdfium 의 `crop=(left, bottom, right, top)` 여백으로 바꿔 쓴다.
  - 워커는 프로세스당 스레드 하나라 잠금이 필요 없다. `~/venv/ocrserver` 에 pypdfium2 5.14.0 을 설치했다.
- `batch_ocr.py`:
  - `--concurrency` 스레드가 각자 문서를 열던 구조라, open+render 를 `_pdf_lock` 아래로 모았다. OCR 은 잠금 밖에서 한다.
- 테스트(`smoke_figures`·`contract_spec_v2`)의 PDF 생성을 `PdfDocument.new()` 빈 페이지로 바꿨다. 테스트는 PDF 안 글자를 쓰지 않는다.
- `scripts/subfigure/reference/figure_panels.py`(fsis 참조 사본)도 같은 방식으로 바꿨다. `render_origin_px` 는 clip × scale 로 계산한다.
- 루트 `Dockerfile`(chandra 이미지, `batch_ocr.py` 용)·`requirements.txt`·README 도 pypdfium2 로 바꿨다. chandra 이미지는 아직 재빌드하지 않았다.

## 검증

- `tests.smoke_figures` 0.3.17 이미지에서 93개 통과.
- 렌더 비교:
  - 대상: `_render_one_page` 를 0.3.16(PyMuPDF)·0.3.17(pdfium) 이미지에서 각각 돌렸다. kruskal1964 + 실제 PDF 24편, 각 첫·가운데·끝 쪽, 73쪽.
  - 25편 모두 열리고 페이지 수가 같다. 크기는 같거나 1 px 차이다. 렌더 시간은 PyMuPDF 9.5 s, pdfium 3.2 s 다.
  - 회색조 평균 차이는 중앙값 1.5/255, 최대 5.4 였다. 눈으로 보면 앤티에일리어싱 차이다.
  - 한 편(`26bea95e…`)은 내장되지 않은 글꼴을 MuPDF 는 Courier 계열, pdfium 은 sans 계열로 대체한다. 글자는 같다.
- 워커 비교: 옛 `render_crop`·`render_target` 과 새것을 kruskal + 6편에서 돌렸다.
  - clip 좌표가 같고 같은 영역이다(y 뒤집힘 없음). 크기 차이는 1–2 px, 차이 1.5–7.5/255 다. 글꼴 대체 편만 13 이다.
- 배포: 0.3.17 `/healthz` ok, `/status` 200, 컨테이너 안에서 pdfium 으로 `/data/pdfs` 를 연다.
- **아직 못 한 것**: chandra 가 정지 중이라 실제 OCR 회귀(`kruskal1964.pdf` 15/15)는 OCR 복구 때 확인한다.

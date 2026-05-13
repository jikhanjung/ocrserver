#!/usr/bin/env python3
"""
/data/pdfs/{hash}.pdf 파일을 스캔해서 DB의 file_hash를 채운다.

- 신규 배포 이전 job (PDF 미저장)은 소급 불가 — 파일이 없으므로 건너뜀
- PDF 저장 후 DB hash가 누락된 경우(재배포/장애 등)를 복구하는 용도

사용법:
  python3 backfill_hashes.py
  python3 backfill_hashes.py --db /srv/ocrserver/data/ocrserver.db \
                              --pdf-dir /srv/ocrserver/data/pdfs
"""
import argparse
import hashlib
import os
import sqlite3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/data/ocrserver.db")
    parser.add_argument("--pdf-dir", default="/data/pdfs")
    args = parser.parse_args()

    if not os.path.isdir(args.pdf_dir):
        print(f"PDF 디렉토리 없음: {args.pdf_dir}")
        return

    con = sqlite3.connect(args.db)

    updated = skipped = errors = 0
    for fname in sorted(os.listdir(args.pdf_dir)):
        if not fname.endswith(".pdf"):
            continue
        stored_hash = fname[:-4]
        path = os.path.join(args.pdf_dir, fname)
        try:
            with open(path, "rb") as f:
                actual_hash = hashlib.sha256(f.read()).hexdigest()
        except OSError as e:
            print(f"  읽기 실패: {fname} — {e}")
            errors += 1
            continue

        if stored_hash != actual_hash:
            print(f"  hash 불일치: {fname} (실제 {actual_hash[:12]}...) — 건너뜀")
            skipped += 1
            continue

        cur = con.execute(
            "UPDATE jobs SET file_hash=? WHERE file_hash IS NULL AND job_id IN "
            "(SELECT job_id FROM jobs WHERE file_hash IS NULL AND filename=?)",
            (actual_hash, fname),
        )
        if cur.rowcount:
            print(f"  업데이트: {actual_hash[:12]}... → {cur.rowcount}건")
            updated += cur.rowcount
        else:
            skipped += 1

    con.commit()
    con.close()
    print(f"\n완료: 업데이트 {updated}건 / 건너뜀 {skipped}건 / 오류 {errors}건")


if __name__ == "__main__":
    main()

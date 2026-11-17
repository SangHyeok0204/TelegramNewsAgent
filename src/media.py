"""미디어 다운로드 — 사진(photo) 메시지를 MEDIA_DIR에 저장하고 DB에 파일명을 기록.

영상/문서/webp(스티커)는 제외하고 '사진'만 받는다(m.photo).
파일이 이미 있으면 다시 받지 않는다(idempotent).
"""
from pathlib import Path

from . import config, db


def download_photos(client, chat_id, conn, messages):
    """messages 중 사진 메시지를 MEDIA_DIR에 저장하고 media_file을 기록한다.

    반환: (새로 받은 건수, 사진 메시지 총수)
    """
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    photo_total = 0
    for m in messages:
        if not m.photo:  # 사진만 (영상/문서/webp 제외)
            continue
        photo_total += 1
        dest = config.MEDIA_DIR / f"{chat_id}_{m.id}.jpg"
        if dest.exists():
            db.set_media_file(conn, chat_id, m.id, dest.name)
            continue
        path = client.download_media(m, file=str(dest))
        if path:
            db.set_media_file(conn, chat_id, m.id, Path(path).name)
            downloaded += 1
    return downloaded, photo_total

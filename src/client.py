"""Telethon 로그인 & 세션 저장 (M1).

최초 1회 실행 시 Telegram 앱으로 받은 로그인 코드(및 2FA 비밀번호)를
콘솔에서 직접 입력해야 한다(사람 게이트). 성공하면 TG_SESSION(*.session)
파일에 로그인 상태가 저장되어 이후 모듈들이 자동 재사용한다.

실행:
    .venv\\Scripts\\python -m src.client
"""
from telethon.sync import TelegramClient  # sync: 동기 컨텍스트에서 메서드 자동 실행

from . import config


def make_client() -> TelegramClient:
    """설정값으로 Telethon 클라이언트를 만든다(다른 모듈에서 재사용)."""
    if not config.API_ID or not config.API_HASH:
        raise SystemExit(
            "[client] .env(회사 PC: 중앙 vault, 단독 PC: 프로젝트 .env)에 "
            "TELEGRAM_BOT__TG_API_ID/TELEGRAM_BOT__TG_API_HASH가 필요합니다. "
            "my.telegram.org에서 발급 후 입력하세요."
        )
    return TelegramClient(config.SESSION, int(config.API_ID), config.API_HASH)


def main():
    client = make_client()
    # 미로그인 시 start()가 콘솔에서 코드/2FA를 입력받는다(사람 게이트)
    client.start(phone=config.PHONE)
    me = client.get_me()
    uname = f"@{me.username}" if me.username else "(username 없음)"
    print(f"[client] 로그인 성공: {me.first_name} {uname} id={me.id}")
    print(f"[client] 세션 저장됨 → {config.SESSION}")
    client.disconnect()


if __name__ == "__main__":
    main()

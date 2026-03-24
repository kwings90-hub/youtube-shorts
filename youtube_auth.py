#!/usr/bin/env python3
"""
YouTube 채널 인증 스크립트
각 채널별로 OAuth 토큰을 발급받아 저장합니다.
"""

import os
import json
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

PROJ_DIR = Path(__file__).parent
CLIENT_SECRET_A = PROJ_DIR / "client_secret.json"    # 계정A: 이슈킥, 찌라핫, 가쉽온, 팩톡
CLIENT_SECRET_B = PROJ_DIR / "client_secret_B.json"   # 계정B: 핫찌, 뉴썰, 핫팩트, 핫이슈랩
TOKEN_DIR = PROJ_DIR / "youtube_auth"

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]

# 채널 → 어떤 client_secret 사용하는지 매핑
CHANNELS = [
    "1.핫찌", "2.뉴썰", "3.이슈킥", "4.찌라핫",
    "5.핫팩트", "6.핫이슈랩", "7.가쉽온", "8.팩톡",
]

# 계정별 채널 매핑
ACCOUNT_A_CHANNELS = {"3.이슈킥", "4.찌라핫", "7.가쉽온", "8.팩톡"}
ACCOUNT_B_CHANNELS = {"1.핫찌", "2.뉴썰", "5.핫팩트", "6.핫이슈랩"}


def get_client_secret(channel_name):
    """채널에 맞는 client_secret 파일 반환"""
    if channel_name in ACCOUNT_A_CHANNELS:
        return CLIENT_SECRET_A
    else:
        return CLIENT_SECRET_B


def get_channel_info(credentials):
    """인증된 계정의 YouTube 채널 이름 가져오기"""
    try:
        youtube = build("youtube", "v3", credentials=credentials)
        resp = youtube.channels().list(part="snippet", mine=True).execute()
        items = resp.get("items", [])
        if items:
            return items[0]["snippet"]["title"]
    except Exception as e:
        print(f"  ⚠️ 채널 정보 조회 실패: {e}")
    return "알 수 없음"


def authenticate_channel(channel_name):
    """단일 채널 인증"""
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    token_path = TOKEN_DIR / f"token_{channel_name}.json"

    creds = None

    # 기존 토큰 확인
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    # 토큰 갱신 또는 새로 발급
    if creds and creds.expired and creds.refresh_token:
        print(f"  🔄 토큰 갱신 중...")
        creds.refresh(Request())
    elif not creds or not creds.valid:
        print(f"  🌐 브라우저에서 로그인하세요...")
        print(f"  ⚠️  '{channel_name}'에 해당하는 YouTube 계정으로 로그인!")
        client_secret = get_client_secret(channel_name)
        if not client_secret.exists():
            print(f"  ❌ {client_secret.name} 파일이 없습니다!")
            return None
        flow = InstalledAppFlow.from_client_secrets_file(
            str(client_secret), SCOPES
        )
        creds = flow.run_local_server(port=0, open_browser=True)

    # 토큰 저장
    with open(token_path, "w") as f:
        f.write(creds.to_json())

    # 채널 확인
    yt_name = get_channel_info(creds)
    print(f"  ✅ 인증 완료! YouTube 채널: {yt_name}")
    return creds


def main():
    if not CLIENT_SECRET_A.exists() and not CLIENT_SECRET_B.exists():
        print("❌ client_secret 파일이 없습니다!")
        return

    print("=" * 50)
    print("  🎬 YouTube 채널 인증")
    print("=" * 50)
    print()

    # 이미 인증된 채널 확인
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    existing = [f.stem.replace("token_", "") for f in TOKEN_DIR.glob("token_*.json")]

    for ch in CHANNELS:
        status = "✅ 인증됨" if ch in existing else "❌ 미인증"
        print(f"  {ch}: {status}")

    print()

    # 인증할 채널 선택
    print("옵션:")
    print("  [번호] 해당 채널만 인증 (예: 1)")
    print("  [a]    미인증 채널 전부 인증")
    print("  [r]    특정 채널 재인증 (예: r3)")
    print("  [q]    종료")
    print()

    choice = input("선택: ").strip().lower()

    if choice == "q":
        return
    elif choice == "a":
        # 미인증 채널 전부
        for ch in CHANNELS:
            if ch not in existing:
                print(f"\n📺 {ch} 인증 시작...")
                authenticate_channel(ch)
    elif choice.startswith("r") and len(choice) > 1:
        # 재인증
        idx = int(choice[1:]) - 1
        if 0 <= idx < len(CHANNELS):
            ch = CHANNELS[idx]
            print(f"\n📺 {ch} 재인증 시작...")
            authenticate_channel(ch)
        else:
            print("잘못된 번호")
    else:
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(CHANNELS):
                ch = CHANNELS[idx]
                print(f"\n📺 {ch} 인증 시작...")
                authenticate_channel(ch)
            else:
                print("잘못된 번호")
        except ValueError:
            print("잘못된 입력")

    print("\n완료!")


if __name__ == "__main__":
    main()

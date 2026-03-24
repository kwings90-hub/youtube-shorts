#!/usr/bin/env python3
"""
YouTube Shorts 업로드 모듈
채널별 토큰을 사용하여 영상을 업로드합니다.
"""

import json
import time
import unicodedata
from pathlib import Path
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

PROJ_DIR = Path(__file__).parent
CLIENT_SECRET_A = PROJ_DIR / "client_secret.json"
CLIENT_SECRET_B = PROJ_DIR / "client_secret_B.json"
TOKEN_DIR = PROJ_DIR / "youtube_auth"

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]

# 계정별 채널 매핑
ACCOUNT_A_CHANNELS = {"3.이슈킥", "4.찌라핫", "7.가쉽온", "8.팩톡"}
ACCOUNT_B_CHANNELS = {"1.핫찌", "2.뉴썰", "5.핫팩트", "6.핫이슈랩"}


def get_credentials(channel_name):
    """채널의 OAuth 토큰 로드 및 갱신"""
    # macOS NFD → NFC 정규화 (토큰 파일은 NFC로 저장됨)
    channel_name_nfc = unicodedata.normalize("NFC", channel_name)
    token_path = TOKEN_DIR / f"token_{channel_name_nfc}.json"
    if not token_path.exists():
        # NFD로도 시도
        token_path_nfd = TOKEN_DIR / f"token_{channel_name}.json"
        if token_path_nfd.exists():
            token_path = token_path_nfd
        else:
            raise FileNotFoundError(f"토큰 없음: {channel_name} (인증 필요)")

    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    # 토큰 갱신
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return creds


def _normalize(name):
    """채널명 NFC 정규화"""
    return unicodedata.normalize("NFC", name)


def upload_video(channel_name, video_path, title, description="", tags=None,
                 category="24", privacy="public", publish_at="",
                 progress_callback=None):
    """
    YouTube에 영상 업로드

    Args:
        channel_name: 채널 폴더명 (예: "3.이슈킥")
        video_path: 영상 파일 경로
        title: 영상 제목
        description: 영상 설명
        tags: 태그 리스트
        category: 카테고리 ID (24=엔터테인먼트)
        privacy: public / unlisted / private
        publish_at: 예약 공개 시간 (ISO 8601, 예: "2026-03-12T09:00:00Z")
        progress_callback: 진행률 콜백 fn(channel_name, percent, status_msg)

    Returns:
        dict: {"ok": True, "video_id": "...", "url": "..."} or {"ok": False, "error": "..."}
    """
    video_path = Path(video_path)
    if not video_path.exists():
        return {"ok": False, "error": f"파일 없음: {video_path}"}

    try:
        if progress_callback:
            progress_callback(channel_name, 0, "인증 중...")

        creds = get_credentials(channel_name)
        youtube = build("youtube", "v3", credentials=creds)

        # Shorts 제목에 #Shorts 추가 (자동 인식 보조)
        if "#Shorts" not in title and "#shorts" not in title:
            title = f"{title} #Shorts"

        status_body = {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        }
        if publish_at:
            status_body["publishAt"] = publish_at

        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags or [],
                "categoryId": category,
            },
            "status": status_body,
        }

        media = MediaFileUpload(
            str(video_path),
            mimetype="video/mp4",
            resumable=True,
            chunksize=10 * 1024 * 1024,  # 10MB 청크
        )

        if progress_callback:
            progress_callback(channel_name, 10, "업로드 시작...")

        request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )

        # 청크 업로드 (진행률 추적)
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 80) + 10  # 10~90%
                if progress_callback:
                    progress_callback(channel_name, pct, f"업로드 중 {int(status.progress()*100)}%...")

        video_id = response["id"]
        video_url = f"https://youtube.com/shorts/{video_id}"

        if progress_callback:
            progress_callback(channel_name, 100, f"✅ 완료!")

        return {"ok": True, "video_id": video_id, "url": video_url}

    except HttpError as e:
        error_msg = str(e)
        if "quotaExceeded" in error_msg:
            error_msg = "API 할당량 초과 (내일 다시 시도)"
        elif "forbidden" in error_msg.lower():
            error_msg = "권한 없음 (재인증 필요)"
        return {"ok": False, "error": error_msg}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_authenticated_channels():
    """인증된 채널 목록 반환"""
    if not TOKEN_DIR.exists():
        return []
    channels = []
    for f in sorted(TOKEN_DIR.glob("token_*.json")):
        ch_name = f.stem.replace("token_", "")
        channels.append(ch_name)
    return channels


def check_token_valid(channel_name):
    """토큰이 유효한지 확인"""
    try:
        creds = get_credentials(channel_name)
        return creds.valid
    except Exception:
        return False

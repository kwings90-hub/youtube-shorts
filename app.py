#!/usr/bin/env python3
"""
유튜브 쇼츠 자동 편집기 - 웹 서버
내부 IP로 접속 가능 (같은 WiFi 내 폰/PC)
"""

import os
import sys
import json
import time
import threading
import shutil
import socket
import queue
import unicodedata
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, Response

# auto_editor 임포트
sys.path.insert(0, str(Path(__file__).parent))

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500MB

# 경로
PROJ_DIR = Path(__file__).parent
BASE_DIR = PROJ_DIR / "original_source"
OUTPUT_DIR = PROJ_DIR / "output"
CHANNEL_DIR = BASE_DIR / "Channel"
SESSIONS_DIR = PROJ_DIR / "sessions"

# ============================================================
# 세션 관리 (업로드 파일 격리)
# ============================================================
current_session_id = None
session_generation_done = False  # 생성 완료 후 다음 업로드 시 새 세션


def get_session_dir():
    """현재 세션 폴더 반환 (없으면 생성)"""
    global current_session_id, session_generation_done
    if current_session_id is None or session_generation_done:
        # 새 세션 생성
        current_session_id = time.strftime("%Y%m%d_%H%M%S")
        session_generation_done = False
        cleanup_old_sessions()
    sess = SESSIONS_DIR / current_session_id
    sess.mkdir(parents=True, exist_ok=True)
    return sess


def get_session_image_dir():
    return get_session_dir() / "image"


def cleanup_old_sessions(max_keep=5):
    """세션 폴더가 max_keep개 초과 시 오래된 것부터 삭제"""
    if not SESSIONS_DIR.exists():
        return
    dirs = sorted([
        d for d in SESSIONS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    ])
    while len(dirs) >= max_keep:
        oldest = dirs.pop(0)
        shutil.rmtree(oldest, ignore_errors=True)

# ============================================================
# 진행 상태 관리 (SSE)
# ============================================================
progress_listeners = []
status = {
    "state": "idle",
    "channel_idx": 0,
    "total_channels": 0,
    "channel_name": "",
    "step": "",
    "percent": 0,
    "videos": [],
    "logs": [],
    "error": None,
}
_generation_lock = threading.Lock()


def broadcast(data):
    for q in progress_listeners[:]:
        try:
            q.put_nowait(data)
        except Exception:
            pass


def update_status(**kw):
    status.update(kw)
    broadcast(dict(status))


def add_log(msg):
    status["logs"].append(msg)
    broadcast({"log": msg})


# ============================================================
# 라우트 - 페이지
# ============================================================
@app.route("/")
def index():
    return HTML_TEMPLATE


# ============================================================
# 라우트 - 기존 파일 확인
# ============================================================
@app.route("/api/check")
def check_files():
    """채널 틀.png만 확인 (대본/이미지/TTS는 매번 새로 업로드)"""
    channels = []
    if CHANNEL_DIR.exists():
        for d in sorted(CHANNEL_DIR.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                has_frame = (d / "틀.png").exists()
                channels.append({"name": d.name, "has_frame": has_frame})
    return jsonify({"channels": channels})


# ============================================================
# 라우트 - 업로드
# ============================================================
@app.route("/api/upload/script", methods=["POST"])
def upload_script():
    text = request.json.get("text", "")
    if not text.strip():
        return jsonify({"ok": False, "error": "빈 대본"}), 400
    sess = get_session_dir()
    sp = sess / "대본_전체.txt"
    with open(sp, "w", encoding="utf-8") as f:
        f.write(text)
    # 파싱 검증
    try:
        import auto_editor as ae
        parsed = ae.parse_script_all(sp)
        return jsonify({"ok": True, "channels": len(parsed),
                        "names": list(parsed.keys()),
                        "session": current_session_id})
    except Exception as e:
        return jsonify({"ok": True, "channels": 0, "error": str(e)})


@app.route("/api/upload/images", methods=["POST"])
def upload_images():
    """이미지 추가 업로드 (세션 폴더에 저장)"""
    img_dir = get_session_image_dir()
    img_dir.mkdir(parents=True, exist_ok=True)
    exts = {".jpg", ".jpeg", ".png", ".webp", ".avif"}
    existing = [f for f in sorted(img_dir.iterdir()) if f.suffix.lower() in exts]
    next_num = len(existing) + 1
    files = request.files.getlist("images")
    saved = []
    for i, file in enumerate(files):
        ext = Path(file.filename).suffix or ".jpg"
        save_path = img_dir / f"{next_num + i:02d}{ext}"
        file.save(str(save_path))
        saved.append(file.filename)
    total = len(existing) + len(saved)
    return jsonify({"ok": True, "count": total, "added": len(saved), "files": saved})


@app.route("/api/delete/image", methods=["POST"])
def delete_image():
    """이미지 1개 삭제 후 번호 재정렬 (세션 폴더)"""
    idx = request.json.get("index", -1)
    img_dir = get_session_image_dir()
    img_dir.mkdir(parents=True, exist_ok=True)
    exts = {".jpg", ".jpeg", ".png", ".webp", ".avif"}
    files = sorted([f for f in img_dir.iterdir() if f.suffix.lower() in exts])
    if idx < 0 or idx >= len(files):
        return jsonify({"ok": False, "error": "잘못된 인덱스"}), 400
    files[idx].unlink()
    remaining = sorted([f for f in img_dir.iterdir() if f.suffix.lower() in exts])
    for i, f in enumerate(remaining):
        new_path = img_dir / f"{i+1:02d}{f.suffix}"
        if f != new_path:
            f.rename(new_path)
    final_count = len(remaining)
    return jsonify({"ok": True, "count": final_count})


@app.route("/api/clear/images", methods=["POST"])
def clear_images():
    """이미지 전체 삭제 (세션 폴더)"""
    img_dir = get_session_image_dir()
    if img_dir.exists():
        for f in img_dir.iterdir():
            if f.is_file():
                f.unlink()
    return jsonify({"ok": True})


@app.route("/api/upload/tts", methods=["POST"])
def upload_tts():
    sess = get_session_dir()
    file = request.files.get("tts")
    if not file:
        return jsonify({"ok": False, "error": "파일 없음"}), 400
    save_path = sess / "TTS.mp3"
    file.save(str(save_path))
    return jsonify({"ok": True, "name": file.filename})


# ============================================================
# 라우트 - 세션 관리
# ============================================================
@app.route("/api/session/new", methods=["POST"])
def new_session():
    """새 세션 시작 (기존 업로드 초기화)"""
    global current_session_id, session_generation_done
    current_session_id = None
    session_generation_done = False
    sess = get_session_dir()  # 새 세션 생성
    return jsonify({"ok": True, "session": current_session_id})


@app.route("/api/session/info")
def session_info():
    return jsonify({"session": current_session_id})


# ============================================================
# 라우트 - 영상 생성
# ============================================================
@app.route("/api/generate", methods=["POST"])
def start_generate():
    if status["state"] == "generating":
        return jsonify({"ok": False, "error": "이미 생성 중"}), 409
    t = threading.Thread(target=run_generation, daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/api/progress")
def progress_stream():
    def stream():
        q = queue.Queue()
        progress_listeners.append(q)
        try:
            yield f"data: {json.dumps(status, ensure_ascii=False)}\n\n"
            while True:
                try:
                    data = q.get(timeout=30)
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            if q in progress_listeners:
                progress_listeners.remove(q)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


# ============================================================
# 라우트 - 영상 서빙 / 목록
# ============================================================
@app.route("/output/<path:filename>")
def serve_output(filename):
    return send_from_directory(str(OUTPUT_DIR), filename)


@app.route("/api/videos")
def list_videos():
    videos = []
    if OUTPUT_DIR.exists():
        for f in sorted(OUTPUT_DIR.glob("*.mp4")):
            videos.append({
                "name": f.name,
                "size_mb": round(f.stat().st_size / (1024 * 1024), 1),
                "url": f"/output/{f.name}",
            })
    return jsonify(videos)


# ============================================================
# 라우트 - YouTube 업로드
# ============================================================
_upload_lock = threading.Lock()

@app.route("/api/youtube/channels")
def youtube_channels():
    """인증된 YouTube 채널 목록"""
    import youtube_uploader as yt
    channels = yt.get_authenticated_channels()
    return jsonify({"channels": channels})


@app.route("/api/youtube/upload", methods=["POST"])
def youtube_upload():
    """YouTube 업로드 시작 (백그라운드)"""
    if status["state"] == "uploading":
        return jsonify({"ok": False, "error": "이미 업로드 중"}), 409
    data = request.json
    videos = data.get("videos", [])  # [{name, title, channel}]
    privacy = data.get("privacy", "public")
    tags = data.get("tags", [])
    publish_at = data.get("publishAt", "")
    if not videos:
        return jsonify({"ok": False, "error": "업로드할 영상 없음"}), 400
    t = threading.Thread(target=run_youtube_upload,
                         args=(videos, privacy, tags, publish_at), daemon=True)
    t.start()
    return jsonify({"ok": True})


def run_youtube_upload(videos, privacy, tags, publish_at):
    if not _upload_lock.acquire(blocking=False):
        return
    try:
        _run_youtube_upload_inner(videos, privacy, tags, publish_at)
    finally:
        _upload_lock.release()


def _run_youtube_upload_inner(videos, privacy, tags, publish_at):
    import youtube_uploader as yt

    status["logs"] = []
    update_status(state="uploading", percent=0, error=None,
                  step="YouTube 업로드 준비...")

    results = []
    total = len(videos)

    for i, v in enumerate(videos):
        ch_name = v["channel"]  # 예: "3.이슈킥"
        video_path = OUTPUT_DIR / v["name"]  # 예: "3.이슈킥_shorts.mp4"
        title = v.get("title", ch_name)

        add_log(f"\n📤 [{i+1}/{total}] {ch_name} → YouTube")
        update_status(step=f"{ch_name} 업로드 중...",
                      channel_name=ch_name,
                      channel_idx=i + 1,
                      total_channels=total,
                      percent=int(i / total * 100))

        def progress_cb(ch, pct, msg):
            overall = int((i / total + pct / 100 / total) * 100)
            update_status(percent=overall, step=f"{ch} - {msg}")
            add_log(f"  {msg}")

        vid_description = v.get("description", title)

        result = yt.upload_video(
            channel_name=ch_name,
            video_path=video_path,
            title=title,
            description=vid_description,
            tags=tags,
            privacy=privacy,
            publish_at=publish_at,
            progress_callback=progress_cb,
        )

        results.append({"channel": ch_name, **result})

        if result["ok"]:
            add_log(f"  ✅ {result['url']}")
        else:
            add_log(f"  ❌ {result['error']}")

    ok_count = sum(1 for r in results if r["ok"])
    add_log(f"\n🎉 업로드 완료! {ok_count}/{total}개 성공")
    update_status(state="upload_done", percent=100,
                  step=f"업로드 완료! {ok_count}/{total}개",
                  error=None)
    broadcast({"upload_results": results})


# ============================================================
# 영상 생성 (백그라운드 스레드)
# ============================================================
def run_generation():
    if not _generation_lock.acquire(blocking=False):
        return
    try:
        _run_generation_inner()
    finally:
        _generation_lock.release()


def _run_generation_inner():
    global session_generation_done
    import auto_editor as ae

    # 현재 세션 폴더에서 파일 읽기
    sess = get_session_dir()
    sess_image_dir = sess / "image"
    sess_script = sess / "대본_전체.txt"
    sess_tts = sess / "TTS.mp3"

    status["logs"] = []
    update_status(state="generating", percent=0, videos=[], error=None)
    add_log(f"📁 세션: {current_session_id}")

    try:
        # 1. 채널 스캔
        add_log("📺 채널 스캔 중...")
        update_status(step="채널 스캔 중...")
        channel_dirs = sorted([
            d for d in CHANNEL_DIR.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        ])
        n_channels = len(channel_dirs)
        update_status(total_channels=n_channels)
        add_log(f"  {n_channels}개 채널 감지")

        # 2. 이미지 로드 (세션 폴더)
        add_log("🖼️ 이미지 로드 + 얼굴 감지...")
        update_status(step="이미지 로드 + 얼굴 감지...")
        ae._face_y_cache.clear()
        images = ae.load_images(sess_image_dir)
        add_log(f"  {len(images)}장 로드 완료")

        # 3. CLIP
        add_log("🤖 CLIP 모델 초기화...")
        update_status(step="CLIP 모델 로드...")
        clip_ok = ae.init_clip()
        clip_embeddings = None
        if clip_ok:
            update_status(step="이미지 임베딩 계산...")
            clip_embeddings = ae.compute_clip_embeddings(images)
            add_log(f"  ✅ CLIP 임베딩 완료 ({len(images)}장)")

        # 4. 폰트
        title_font = ae.load_font(ae.FONT_PATH, ae.TITLE_FONT_SIZE, index=ae.TITLE_FONT_INDEX)
        subtitle_font = ae.load_font(ae.FONT_PATH, ae.SUBTITLE_FONT_SIZE, index=ae.SUBTITLE_FONT_INDEX)

        # 5. 대본 (세션 폴더)
        update_status(step="대본 로드...")
        all_scripts = None
        if sess_script.exists():
            all_scripts = ae.parse_script_all(sess_script)
            add_log(f"📄 통합 대본: {len(all_scripts)}개 채널")

        # 6. TTS 분할 (세션 폴더)
        update_status(step="TTS 분할...")
        add_log("✂️ TTS 묵음 분할...")
        audio_segments = ae.split_tts_by_silence(sess_tts, n_channels)
        n_channels = min(n_channels, len(audio_segments))
        add_log(f"  {len(audio_segments)}개 세그먼트")

        # 7. 채널별 생성
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        success_count = 0
        videos = []

        for ch_idx in range(n_channels):
            ch_dir = channel_dirs[ch_idx]
            ch_name = ch_dir.name
            ch_name_nfc = unicodedata.normalize("NFC", ch_name)
            config = ae.CHANNEL_CONFIG.get(ch_name_nfc, {
                "subtitle_color": (0, 0, 0),
                "headline_color": (0, 0, 0),
            })

            pct = int((ch_idx / n_channels) * 100)
            add_log(f"\n{'='*40}")
            add_log(f"📺 [{ch_idx+1}/{n_channels}] {ch_name}")
            update_status(channel_idx=ch_idx + 1, channel_name=ch_name, percent=pct)

            # 대본
            update_status(step=f"{ch_name} - 대본 파싱")
            if all_scripts and ch_name_nfc in all_scripts:
                headline, script_text = all_scripts[ch_name_nfc]
            elif (ch_dir / "대본.txt").exists():
                headline, script_text = ae.parse_script(ch_dir / "대본.txt")
            else:
                add_log(f"  ⚠️ 대본 없음 → 건너뜀")
                continue

            # Whisper
            update_status(step=f"{ch_name} - Whisper 싱크")
            add_log(f"  🎙️ Whisper 싱크...")
            audio_path = audio_segments[ch_idx]
            timings = ae.get_whisper_timings(audio_path, script_text)

            # CLIP 매칭
            update_status(step=f"{ch_name} - 이미지 매칭")
            add_log(f"  🔗 이미지 매칭...")
            image_assignments = ae.assign_images(timings, images, clip_embeddings)

            # 프레임 오버레이
            from PIL import Image as PILImage
            frame_overlay = PILImage.open(ch_dir / "틀.png").convert("RGBA")
            if frame_overlay.size != (ae.WIDTH, ae.HEIGHT):
                frame_overlay = frame_overlay.resize((ae.WIDTH, ae.HEIGHT), PILImage.LANCZOS)

            # 프레임 생성
            update_status(step=f"{ch_name} - 프레임 생성")
            add_log(f"  📸 프레임 생성...")
            ae._resize_cache.clear()
            audio_duration = ae.get_audio_duration(audio_path)
            total_frames = ae.generate_frames(
                headline, timings, images, image_assignments,
                frame_overlay, title_font, subtitle_font,
                audio_duration=audio_duration,
                headline_color=config["headline_color"],
                subtitle_color=config["subtitle_color"],
                subtitle_stroke=config.get("subtitle_stroke"),
            )

            # 영상 합성
            update_status(step=f"{ch_name} - 영상 합성")
            add_log(f"  🎥 영상 합성...")
            output_path = OUTPUT_DIR / f"{ch_name}_shorts.mp4"
            success = ae.assemble_video(total_frames, audio_path, output_path)

            if success:
                success_count += 1
                size_mb = round(output_path.stat().st_size / (1024 * 1024), 1)
                videos.append({
                    "name": output_path.name,
                    "size_mb": size_mb,
                    "url": f"/output/{output_path.name}",
                    "headline": headline,
                    "channel": ch_name,
                })
                add_log(f"  ✅ 완료 ({size_mb}MB)")
                ae.cleanup_temp_frames()

            update_status(videos=videos)

        ae.cleanup_temp()
        session_generation_done = True  # 다음 업로드 시 새 세션 생성
        add_log(f"\n🎉 완성! {success_count}/{n_channels}채널")
        update_status(state="done", percent=100,
                      step=f"완료! {success_count}/{n_channels}채널",
                      videos=videos)

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        add_log(f"❌ 오류: {e}\n{tb}")
        update_status(state="error", error=str(e), step=f"오류 발생")


# ============================================================
# 로컬 IP
# ============================================================
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ============================================================
# HTML 템플릿
# ============================================================
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>쇼츠 자동 편집기</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: -apple-system, 'Apple SD Gothic Neo', sans-serif;
       background: #0f172a; color: #e2e8f0; min-height: 100vh; }

.container { max-width: 1200px; margin: 0 auto; padding: 20px; }

header { text-align: center; padding: 30px 0 20px; }
header h1 { font-size: 28px; background: linear-gradient(135deg, #a78bfa, #f472b6);
             -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
header p { color: #94a3b8; margin-top: 8px; font-size: 14px; }

/* 업로드 그리드 */
.upload-grid { display: grid; grid-template-columns: 1fr 1fr 1fr;
               gap: 16px; margin: 20px 0; }
@media (max-width: 768px) { .upload-grid { grid-template-columns: 1fr; } }

.card { background: #1e293b; border-radius: 12px; padding: 20px;
        border: 1px solid #334155; }
.card h2 { font-size: 16px; margin-bottom: 12px; color: #cbd5e1; }

textarea { width: 100%; height: 200px; background: #0f172a; border: 1px solid #475569;
           border-radius: 8px; padding: 12px; color: #e2e8f0; font-size: 13px;
           resize: vertical; font-family: inherit; }
textarea:focus { outline: none; border-color: #7c3aed; }
textarea::placeholder { color: #64748b; }

.dropzone { border: 2px dashed #475569; border-radius: 8px; padding: 30px;
            text-align: center; cursor: pointer; transition: all 0.2s; }
.dropzone:hover, .dropzone.dragover { border-color: #7c3aed; background: #1a1f35; }
.dropzone p { color: #94a3b8; font-size: 13px; line-height: 1.6; }
.dropzone .icon { font-size: 32px; margin-bottom: 8px; }

.status-badge { display: inline-block; padding: 4px 12px; border-radius: 20px;
                font-size: 12px; margin-top: 10px; }
.status-ok { background: #065f46; color: #6ee7b7; }
.status-wait { background: #78350f; color: #fbbf24; }

.thumb-grid { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.thumb-item { position: relative; display: inline-block; }
.thumb-item img { width: 50px; height: 50px; object-fit: cover; border-radius: 6px;
                  border: 1px solid #475569; }
.thumb-item .thumb-del { position: absolute; top: -4px; right: -4px; width: 18px; height: 18px;
                         background: #ef4444; color: white; border: none; border-radius: 50%;
                         font-size: 11px; line-height: 18px; text-align: center; cursor: pointer;
                         padding: 0; display: flex; align-items: center; justify-content: center; }
.thumb-item .thumb-del:hover { background: #dc2626; }

.cancel-btn { background: #475569; color: #e2e8f0; border: none; padding: 6px 14px;
              border-radius: 6px; cursor: pointer; font-size: 12px; margin-top: 8px; }
.cancel-btn:hover { background: #ef4444; }

/* 생성 버튼 */
.gen-section { text-align: center; margin: 24px 0; }
.gen-btn { padding: 16px 48px; font-size: 18px; font-weight: 700;
           background: linear-gradient(135deg, #7c3aed, #db2777); color: white;
           border: none; border-radius: 12px; cursor: pointer;
           transition: all 0.2s; }
.gen-btn:hover { transform: translateY(-2px); box-shadow: 0 8px 25px rgba(124,58,237,0.4); }
.gen-btn:disabled { opacity: 0.4; cursor: not-allowed; transform: none; box-shadow: none; }

/* 프로그레스 */
.progress-section { margin: 20px auto; max-width: 600px; display: none; }
.progress-bar { height: 8px; background: #334155; border-radius: 4px; overflow: hidden; }
.progress-fill { height: 100%; background: linear-gradient(90deg, #7c3aed, #db2777);
                 border-radius: 4px; transition: width 0.3s; width: 0%; }
.progress-text { text-align: center; margin-top: 10px; color: #94a3b8; font-size: 14px; }
.progress-channel { text-align: center; font-size: 20px; font-weight: 700;
                    margin-bottom: 12px; color: #a78bfa; }

/* 로그 */
.log-section { margin: 20px 0; display: none; }
.log-toggle { background: none; border: 1px solid #475569; color: #94a3b8;
              padding: 8px 16px; border-radius: 8px; cursor: pointer; font-size: 13px; }
.log-box { background: #0f172a; border: 1px solid #334155; border-radius: 8px;
           padding: 12px; margin-top: 10px; max-height: 300px; overflow-y: auto;
           font-family: 'SF Mono', monospace; font-size: 12px; color: #94a3b8;
           line-height: 1.6; white-space: pre-wrap; display: none; }

/* 비디오 그리드 */
.videos-section { margin: 30px 0; display: none; }
.videos-section h2 { font-size: 20px; margin-bottom: 16px; color: #cbd5e1; }
.video-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
@media (max-width: 900px) { .video-grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 500px) { .video-grid { grid-template-columns: 1fr; } }

.video-card { background: #1e293b; border-radius: 12px; overflow: hidden;
              border: 1px solid #334155; }
.video-card video { width: 100%; aspect-ratio: 9/16; background: #000; }
.video-info { padding: 10px 12px; }
.video-info .name { font-size: 14px; font-weight: 600; }
.video-info .size { font-size: 12px; color: #64748b; margin-top: 2px; }

/* 완료 배너 */
.done-banner { background: linear-gradient(135deg, #065f46, #064e3b);
               border: 1px solid #10b981; border-radius: 12px; padding: 20px;
               text-align: center; margin: 20px 0; display: none; }
.done-banner h3 { color: #6ee7b7; font-size: 20px; }
.done-banner p { color: #a7f3d0; font-size: 14px; margin-top: 6px; }

/* TTS 오디오 프리뷰 */
audio { width: 100%; margin-top: 10px; height: 36px; }

/* YouTube 업로드 섹션 */
.yt-section { margin: 30px 0; display: none; }
.yt-section h2 { font-size: 20px; margin-bottom: 16px; color: #cbd5e1; }
.yt-settings { background: #1e293b; border-radius: 12px; padding: 20px;
               border: 1px solid #334155; margin-bottom: 16px; }
.yt-settings label { display: block; color: #94a3b8; font-size: 13px; margin-bottom: 4px; margin-top: 12px; }
.yt-settings label:first-child { margin-top: 0; }
.yt-settings input, .yt-settings select, .yt-settings textarea {
  width: 100%; background: #0f172a; border: 1px solid #475569; border-radius: 8px;
  padding: 10px 12px; color: #e2e8f0; font-size: 13px; font-family: inherit; }
.yt-settings input:focus, .yt-settings select:focus, .yt-settings textarea:focus {
  outline: none; border-color: #7c3aed; }
.yt-channel-list { margin: 12px 0; }
.yt-channel-item { display: flex; align-items: center; gap: 10px; padding: 8px 12px;
                   background: #0f172a; border-radius: 8px; margin-bottom: 6px;
                   border: 1px solid #334155; }
.yt-channel-item input[type="checkbox"] { accent-color: #7c3aed; width: 16px; height: 16px; }
.yt-channel-item .ch-name { font-size: 14px; font-weight: 600; flex: 1; }
.yt-channel-item .ch-title { flex: 2; }
.yt-channel-item .ch-title input { padding: 6px 8px; font-size: 12px; }
.yt-upload-btn { padding: 14px 40px; font-size: 16px; font-weight: 700;
                 background: linear-gradient(135deg, #dc2626, #ea580c); color: white;
                 border: none; border-radius: 12px; cursor: pointer;
                 transition: all 0.2s; margin-top: 16px; }
.yt-upload-btn:hover { transform: translateY(-2px); box-shadow: 0 8px 25px rgba(220,38,38,0.4); }
.yt-upload-btn:disabled { opacity: 0.4; cursor: not-allowed; transform: none; box-shadow: none; }
.yt-result { margin-top: 8px; font-size: 13px; }
.yt-result a { color: #60a5fa; text-decoration: none; }
.yt-result a:hover { text-decoration: underline; }
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>쇼츠 자동 편집기</h1>
    <p>대본 + 이미지 + TTS &rarr; 8채널 영상 자동 생성</p>
    <div style="margin-top:10px;">
      <button class="cancel-btn" style="background:#7c3aed;padding:8px 20px;font-size:13px;" onclick="newSession()">&#x1F504; 새로 시작</button>
      <span id="sessionBadge" style="color:#64748b;font-size:12px;margin-left:10px;"></span>
    </div>
  </header>

  <!-- 업로드 -->
  <div class="upload-grid">
    <!-- 대본 -->
    <div class="card">
      <h2>&#x1F4DD; 대본</h2>
      <div style="margin-bottom:8px;">
        <a href="https://claude.ai/public/artifacts/17b810b3-383e-4cd7-8e87-687e3b3fd1d2" target="_blank" style="display:inline-block; padding:6px 14px; background:#f59e0b; color:#000; border-radius:6px; text-decoration:none; font-weight:bold; font-size:13px;">&#x270F; 대본제작</a>
      </div>
      <div style="display:flex; gap:8px; margin-bottom:8px;">
        <button class="cancel-btn" style="margin:0; background:#7c3aed;" onclick="document.getElementById('scriptFileInput').click()">&#x1F4C4; TXT 파일 열기</button>
        <button class="cancel-btn" style="margin:0;" id="scriptClearBtn" onclick="clearScript()">&#x2715; 초기화</button>
      </div>
      <input type="file" id="scriptFileInput" accept=".txt,text/plain" hidden>
      <textarea id="scriptInput" placeholder="대본_전체 내용을 붙여넣기...&#10;또는 위 버튼으로 TXT 파일 열기&#10;&#10;[1.핫찌]&#10;[헤드라인]&#10;헤드라인 텍스트&#10;&#10;[대본]&#10;대본 텍스트&#10;&#10;[2.뉴썰]&#10;..."></textarea>
      <div id="scriptStatus"></div>
    </div>

    <!-- 이미지 -->
    <div class="card">
      <h2>&#x1F5BC; 이미지</h2>
      <div style="margin-bottom:8px;">
        <a href="https://google-image-crawler.onrender.com/" target="_blank" style="display:inline-block; padding:6px 14px; background:#3b82f6; color:#fff; border-radius:6px; text-decoration:none; font-weight:bold; font-size:13px;">&#x1F50D; 이미지 찾기</a>
      </div>
      <div class="dropzone" id="imageDropzone" onclick="document.getElementById('imageInput').click()">
        <div class="icon">&#x1F4F7;</div>
        <p>이미지를 드래그 앤 드롭<br>또는 클릭하여 선택</p>
      </div>
      <input type="file" id="imageInput" multiple accept="image/*" hidden>
      <div class="thumb-grid" id="imageThumbs"></div>
      <div id="imageStatus"></div>
    </div>

    <!-- TTS -->
    <div class="card">
      <h2>&#x1F3A4; TTS</h2>
      <div style="margin-bottom:8px;">
        <a href="https://typecast.ai/text-to-speech/6944da8aaf8d60e9c435055b" target="_blank" style="display:inline-block; padding:6px 14px; background:#10b981; color:#fff; border-radius:6px; text-decoration:none; font-weight:bold; font-size:13px;">&#x1F3A4; TTS 제작</a>
      </div>
      <div class="dropzone" id="ttsDropzone" onclick="document.getElementById('ttsInput').click()">
        <div class="icon">&#x1F3B5;</div>
        <p>TTS 파일을 드래그 앤 드롭<br>또는 클릭하여 선택</p>
      </div>
      <input type="file" id="ttsInput" accept="audio/*" hidden>
      <audio id="ttsPreview" controls style="display:none"></audio>
      <button class="cancel-btn" id="ttsCancelBtn" style="display:none" onclick="cancelTTS()">&#x2715; TTS 취소</button>
      <div id="ttsStatus"></div>
    </div>
  </div>

  <!-- 생성 -->
  <div class="gen-section">
    <button class="gen-btn" id="genBtn" onclick="startGenerate()">
      &#x1F3AC; 영상 생성 시작
    </button>
  </div>

  <!-- 프로그레스 -->
  <div class="progress-section" id="progressSection">
    <div class="progress-channel" id="progressChannel"></div>
    <div class="progress-bar"><div class="progress-fill" id="progressFill"></div></div>
    <div class="progress-text" id="progressText"></div>
  </div>

  <!-- 완료 배너 -->
  <div class="done-banner" id="doneBanner">
    <h3>&#x2705; 영상 생성 완료!</h3>
    <p id="doneText"></p>
  </div>

  <!-- 로그 -->
  <div class="log-section" id="logSection">
    <button class="log-toggle" onclick="toggleLog()">&#x1F4CB; 로그 보기</button>
    <pre class="log-box" id="logBox"></pre>
  </div>

  <!-- 비디오 -->
  <div class="videos-section" id="videosSection">
    <h2>&#x1F4F9; 완성 영상</h2>
    <div class="video-grid" id="videoGrid"></div>
  </div>

  <!-- YouTube 업로드 -->
  <div class="yt-section" id="ytSection">
    <h2>&#x1F4E4; YouTube 업로드</h2>
    <div class="yt-settings">
      <label>공개 설정</label>
      <select id="ytPrivacy" onchange="toggleSchedule()">
        <option value="public">즉시 공개</option>
        <option value="scheduled">예약 공개</option>
      </select>

      <div id="scheduleRow" style="display:none;">
        <label>예약 시간</label>
        <input type="datetime-local" id="ytSchedule">
      </div>

      <label>태그 (쉼표로 구분)</label>
      <input id="ytTags" type="text" placeholder="예: 쇼츠,핫이슈,뉴스">

      <label>업로드할 채널 &amp; 제목</label>
      <div class="yt-channel-list" id="ytChannelList"></div>
    </div>
    <div style="text-align:center;">
      <button class="yt-upload-btn" id="ytUploadBtn" onclick="startYTUpload()">
        &#x1F680; YouTube 업로드 시작
      </button>
    </div>
    <div id="ytResults"></div>
  </div>
</div>

<script>
// ---- 상태 ----
let scriptReady = false, imageReady = false, ttsReady = false;
let evtSource = null;
let currentSession = null;

function updateSessionBadge(id) {
  currentSession = id;
  const badge = document.getElementById('sessionBadge');
  if (id) badge.textContent = '세션: ' + id;
  else badge.textContent = '';
}

async function newSession() {
  try {
    const res = await fetch('/api/session/new', {method:'POST'});
    const d = await res.json();
    updateSessionBadge(d.session);
  } catch(e) {}
  // UI 초기화
  scriptReady = false; imageReady = false; ttsReady = false;
  document.getElementById('scriptInput').value = '';
  document.getElementById('scriptStatus').innerHTML = '';
  imageThumbs = []; serverImageCount = 0;
  renderImageThumbs();
  document.getElementById('imageStatus').innerHTML = '';
  cancelTTS();
  document.getElementById('doneBanner').style.display = 'none';
  document.getElementById('videosSection').style.display = 'none';
  document.getElementById('progressSection').style.display = 'none';
  document.getElementById('logSection').style.display = 'none';
  updateGenBtn();
}

// ---- 초기 로드: 채널 틀.png만 확인 ----
window.addEventListener('load', async () => {
  try {
    const res = await fetch('/api/check');
    const d = await res.json();
    if (d.channels && d.channels.length > 0) {
      const ok = d.channels.filter(c => c.has_frame).length;
      console.log('채널 ' + ok + '/' + d.channels.length + '개 틀.png 준비됨');
    }
  } catch(e) {}
  // 현재 세션 확인
  try {
    const res = await fetch('/api/session/info');
    const d = await res.json();
    updateSessionBadge(d.session);
  } catch(e) {}
  updateGenBtn();
});

// ---- 대본: 붙여넣기 또는 TXT 파일 ----
let saveTimer = null;
document.getElementById('scriptInput').addEventListener('input', () => {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveScript, 800);
});

// TXT 파일 열기 (버튼)
document.getElementById('scriptFileInput').addEventListener('change', (ev) => {
  loadScriptFile(ev.target.files[0]);
  ev.target.value = '';
});

// TXT 파일 드래그 앤 드롭 (textarea 위에)
const scriptInput = document.getElementById('scriptInput');
scriptInput.addEventListener('dragover', (ev) => { ev.preventDefault(); });
scriptInput.addEventListener('drop', (ev) => {
  ev.preventDefault();
  const file = ev.dataTransfer.files[0];
  if (file && (file.name.endsWith('.txt') || file.type === 'text/plain')) {
    loadScriptFile(file);
  }
});

function loadScriptFile(file) {
  if (!file) return;
  const reader = new FileReader();
  reader.onload = (e) => {
    document.getElementById('scriptInput').value = e.target.result;
    saveScript();
  };
  reader.readAsText(file, 'UTF-8');
}

function clearScript() {
  document.getElementById('scriptInput').value = '';
  scriptReady = false;
  document.getElementById('scriptStatus').innerHTML = '';
  updateGenBtn();
}

async function saveScript() {
  const text = document.getElementById('scriptInput').value.trim();
  if (!text) { scriptReady = false; showStatus('scriptStatus','wait','대본을 입력하세요'); updateGenBtn(); return; }
  showStatus('scriptStatus', 'wait', '저장 중...');
  try {
    const res = await fetch('/api/upload/script', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({text})
    });
    const d = await res.json();
    if (d.ok) {
      scriptReady = true;
      showStatus('scriptStatus', 'ok', d.channels + '개 채널 파싱 완료');
      if (d.session) updateSessionBadge(d.session);
    }
  } catch(e) { showStatus('scriptStatus','wait','저장 실패'); }
  updateGenBtn();
}

// ---- 이미지 업로드 ----
const imageDropzone = document.getElementById('imageDropzone');
const imageInput = document.getElementById('imageInput');
let imageThumbs = []; // {name, url} 클라이언트 프리뷰용
let serverImageCount = 0;

['dragenter','dragover'].forEach(e => imageDropzone.addEventListener(e, ev => {
  ev.preventDefault(); imageDropzone.classList.add('dragover');
}));
['dragleave','drop'].forEach(e => imageDropzone.addEventListener(e, ev => {
  ev.preventDefault(); imageDropzone.classList.remove('dragover');
}));
imageDropzone.addEventListener('drop', ev => { addImages(ev.dataTransfer.files); });
imageInput.addEventListener('change', ev => { addImages(ev.target.files); imageInput.value=''; });

async function addImages(files) {
  if (!files.length) return;
  showStatus('imageStatus', 'wait', '업로드 중...');
  // 로컬 프리뷰 추가
  Array.from(files).forEach(f => {
    imageThumbs.push({name: f.name, url: URL.createObjectURL(f)});
  });
  renderImageThumbs();
  // 서버 업로드
  const fd = new FormData();
  Array.from(files).forEach(f => fd.append('images', f));
  try {
    const res = await fetch('/api/upload/images', {method:'POST', body: fd});
    const d = await res.json();
    if (d.ok) { serverImageCount = d.count; imageReady = true; showStatus('imageStatus','ok', d.count+'장 업로드 완료'); }
    else { showStatus('imageStatus','wait','업로드 실패'); }
  } catch(e) { showStatus('imageStatus','wait','업로드 실패'); }
  updateGenBtn();
}

async function removeImage(idx) {
  // 서버에서 삭제
  try {
    const res = await fetch('/api/delete/image', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({index: idx})
    });
    const d = await res.json();
    if (d.ok) {
      serverImageCount = d.count;
      imageThumbs.splice(idx, 1);
      renderImageThumbs();
      if (d.count === 0) {
        imageReady = false;
        showStatus('imageStatus', 'wait', '이미지를 업로드하세요');
      } else {
        imageReady = true;
        showStatus('imageStatus', 'ok', d.count + '장 남음');
      }
    }
  } catch(e) { showStatus('imageStatus','wait','삭제 실패'); }
  updateGenBtn();
}

function renderImageThumbs() {
  const thumbs = document.getElementById('imageThumbs');
  thumbs.innerHTML = '';
  imageThumbs.forEach((item, i) => {
    const wrap = document.createElement('div');
    wrap.className = 'thumb-item';
    const img = document.createElement('img');
    img.src = item.url;
    const del = document.createElement('button');
    del.className = 'thumb-del';
    del.innerHTML = '&times;';
    del.onclick = (e) => { e.stopPropagation(); removeImage(i); };
    wrap.appendChild(img);
    wrap.appendChild(del);
    thumbs.appendChild(wrap);
  });
}

// ---- TTS 업로드 ----
const ttsDropzone = document.getElementById('ttsDropzone');
const ttsInput = document.getElementById('ttsInput');

['dragenter','dragover'].forEach(e => ttsDropzone.addEventListener(e, ev => {
  ev.preventDefault(); ttsDropzone.classList.add('dragover');
}));
['dragleave','drop'].forEach(e => ttsDropzone.addEventListener(e, ev => {
  ev.preventDefault(); ttsDropzone.classList.remove('dragover');
}));
ttsDropzone.addEventListener('drop', ev => { uploadTTS(ev.dataTransfer.files[0]); });
ttsInput.addEventListener('change', ev => { uploadTTS(ev.target.files[0]); });

async function uploadTTS(file) {
  if (!file) return;
  showStatus('ttsStatus', 'wait', '업로드 중...');
  const audio = document.getElementById('ttsPreview');
  audio.src = URL.createObjectURL(file);
  audio.style.display = 'block';
  document.getElementById('ttsCancelBtn').style.display = 'inline-block';
  const fd = new FormData();
  fd.append('tts', file);
  try {
    const res = await fetch('/api/upload/tts', {method:'POST', body: fd});
    const d = await res.json();
    if (d.ok) { ttsReady = true; showStatus('ttsStatus','ok', d.name + ' 업로드 완료'); }
  } catch(e) { showStatus('ttsStatus','wait','업로드 실패'); }
  updateGenBtn();
}

function cancelTTS() {
  ttsReady = false;
  const audio = document.getElementById('ttsPreview');
  audio.pause(); audio.src = ''; audio.style.display = 'none';
  document.getElementById('ttsCancelBtn').style.display = 'none';
  document.getElementById('ttsStatus').innerHTML = '';
  document.getElementById('ttsInput').value = '';
  updateGenBtn();
}

// ---- 영상 생성 ----
async function startGenerate() {
  const btn = document.getElementById('genBtn');
  btn.disabled = true;
  btn.textContent = '생성 중...';
  document.getElementById('progressSection').style.display = 'block';
  document.getElementById('logSection').style.display = 'block';
  document.getElementById('doneBanner').style.display = 'none';
  document.getElementById('videosSection').style.display = 'none';
  document.getElementById('logBox').textContent = '';

  // SSE 연결
  if (evtSource) evtSource.close();
  evtSource = new EventSource('/api/progress');
  evtSource.onmessage = (e) => {
    try {
      const d = JSON.parse(e.data);
      if (d.log) {
        appendLog(d.log);
        return;
      }
      // 프로그레스 업데이트
      document.getElementById('progressFill').style.width = d.percent + '%';
      document.getElementById('progressText').textContent = d.step || '';
      if (d.channel_name) {
        document.getElementById('progressChannel').textContent =
          '[' + d.channel_idx + '/' + d.total_channels + '] ' + d.channel_name;
      }
      // 완료
      if (d.state === 'done') {
        evtSource.close();
        btn.disabled = false;
        btn.textContent = '\\u{1F3AC} 영상 생성 시작';
        document.getElementById('doneBanner').style.display = 'block';
        document.getElementById('doneText').textContent = d.step;
        if (d.videos && d.videos.length) showVideos(d.videos);
      }
      // 에러
      if (d.state === 'error') {
        evtSource.close();
        btn.disabled = false;
        btn.textContent = '\\u{1F3AC} 다시 시도';
        document.getElementById('progressText').textContent = '오류: ' + d.error;
      }
    } catch(err) {}
  };

  // 생성 트리거
  try { await fetch('/api/generate', {method:'POST'}); } catch(e) {}
}

// ---- 비디오 표시 ----
let generatedVideos = [];

function showVideos(videos) {
  generatedVideos = videos;
  const section = document.getElementById('videosSection');
  const grid = document.getElementById('videoGrid');
  section.style.display = 'block';
  grid.innerHTML = '';
  videos.forEach(v => {
    const card = document.createElement('div');
    card.className = 'video-card';
    card.innerHTML = '<video src="' + v.url + '" controls playsinline preload="metadata"></video>' +
      '<div class="video-info"><div class="name">' + v.name + '</div>' +
      '<div class="size">' + v.size_mb + ' MB</div></div>';
    grid.appendChild(card);
  });
  // YouTube 업로드 섹션 표시
  showYTSection(videos);
}

// ---- YouTube 업로드 ----
function showYTSection(videos) {
  const section = document.getElementById('ytSection');
  const list = document.getElementById('ytChannelList');
  section.style.display = 'block';
  list.innerHTML = '';

  videos.forEach((v, i) => {
    const chName = v.channel || v.name.replace('_shorts.mp4', '');
    const headline = v.headline || '';

    const item = document.createElement('div');
    item.className = 'yt-channel-item';
    item.innerHTML =
      '<input type="checkbox" checked id="ytCh' + i + '" data-channel="' + chName + '" data-headline="' + headline.replace(/"/g, '&quot;') + '">' +
      '<span class="ch-name">' + chName + '</span>' +
      '<span class="ch-title"><input type="text" id="ytTitle' + i + '" value="' + headline.replace(/"/g, '&quot;') + '" placeholder="영상 제목"></span>';
    list.appendChild(item);
  });

  document.getElementById('ytResults').innerHTML = '';
}

function toggleSchedule() {
  const val = document.getElementById('ytPrivacy').value;
  document.getElementById('scheduleRow').style.display = val === 'scheduled' ? 'block' : 'none';
  if (val === 'scheduled' && !document.getElementById('ytSchedule').value) {
    // 기본값: 내일 오전 9시
    const d = new Date();
    d.setDate(d.getDate() + 1);
    d.setHours(9, 0, 0, 0);
    document.getElementById('ytSchedule').value = d.toISOString().slice(0, 16);
  }
}

async function startYTUpload() {
  const btn = document.getElementById('ytUploadBtn');
  btn.disabled = true;
  btn.textContent = '업로드 중...';
  document.getElementById('ytResults').innerHTML = '';

  const privacyVal = document.getElementById('ytPrivacy').value;
  const privacy = privacyVal === 'scheduled' ? 'private' : 'public';
  let publishAt = '';
  if (privacyVal === 'scheduled') {
    const dt = document.getElementById('ytSchedule').value;
    if (!dt) { alert('예약 시간을 선택하세요'); btn.disabled = false; btn.textContent = '\\u{1F680} YouTube 업로드 시작'; return; }
    publishAt = new Date(dt).toISOString();
  }
  const tagsStr = document.getElementById('ytTags').value;
  const tags = tagsStr ? tagsStr.split(',').map(t => t.trim()).filter(Boolean) : [];

  const videos = [];
  generatedVideos.forEach((v, i) => {
    const cb = document.getElementById('ytCh' + i);
    if (cb && cb.checked) {
      const titleInput = document.getElementById('ytTitle' + i);
      const title = titleInput.value.trim() || cb.dataset.headline || cb.dataset.channel;
      videos.push({
        name: v.name,
        channel: cb.dataset.channel,
        title: title,
        description: title,
      });
    }
  });

  if (videos.length === 0) {
    btn.disabled = false;
    btn.textContent = '\\u{1F680} YouTube 업로드 시작';
    alert('업로드할 채널을 선택하세요');
    return;
  }

  // SSE 재연결
  if (evtSource) evtSource.close();
  evtSource = new EventSource('/api/progress');
  document.getElementById('progressSection').style.display = 'block';

  evtSource.onmessage = (e) => {
    try {
      const d = JSON.parse(e.data);
      if (d.log) { appendLog(d.log); return; }
      if (d.upload_results) {
        showYTResults(d.upload_results);
        return;
      }
      document.getElementById('progressFill').style.width = d.percent + '%';
      document.getElementById('progressText').textContent = d.step || '';
      if (d.channel_name) {
        document.getElementById('progressChannel').textContent =
          '[' + d.channel_idx + '/' + d.total_channels + '] ' + d.channel_name + ' 업로드';
      }
      if (d.state === 'upload_done') {
        evtSource.close();
        btn.disabled = false;
        btn.textContent = '\\u{1F680} YouTube 업로드 시작';
        document.getElementById('doneBanner').style.display = 'block';
        document.getElementById('doneText').textContent = d.step;
      }
      if (d.state === 'error') {
        evtSource.close();
        btn.disabled = false;
        btn.textContent = '\\u{1F680} 다시 시도';
      }
    } catch(err) {}
  };

  try {
    await fetch('/api/youtube/upload', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ videos, privacy, tags, publishAt })
    });
  } catch(e) {}
}

function showYTResults(results) {
  const div = document.getElementById('ytResults');
  let html = '<div style="margin-top:16px;">';
  results.forEach(r => {
    if (r.ok) {
      html += '<div class="yt-result" style="color:#6ee7b7;">&#x2705; ' + r.channel +
              ' → <a href="' + r.url + '" target="_blank">' + r.url + '</a></div>';
    } else {
      html += '<div class="yt-result" style="color:#fca5a5;">&#x274C; ' + r.channel +
              ' → ' + r.error + '</div>';
    }
  });
  html += '</div>';
  div.innerHTML = html;
}

// ---- 헬퍼 ----
function showStatus(id, type, msg) {
  document.getElementById(id).innerHTML =
    '<span class="status-badge status-' + type + '">' + msg + '</span>';
}

function updateGenBtn() {
  document.getElementById('genBtn').disabled = !(scriptReady && imageReady && ttsReady);
}

function appendLog(msg) {
  const box = document.getElementById('logBox');
  box.textContent += msg + '\\n';
  box.scrollTop = box.scrollHeight;
}

function toggleLog() {
  const box = document.getElementById('logBox');
  box.style.display = box.style.display === 'none' ? 'block' : 'none';
}
</script>
</body>
</html>
"""

# ============================================================
# 실행
# ============================================================
if __name__ == "__main__":
    port = 8000
    ip = get_local_ip()
    print("=" * 50)
    print("  🎬 쇼츠 자동 편집기 웹 서버")
    print("=" * 50)
    print(f"  로컬:    http://localhost:{port}")
    print(f"  네트워크: http://{ip}:{port}")
    print(f"  (같은 WiFi 내 폰/PC에서 위 주소로 접속)")
    print("=" * 50)
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)

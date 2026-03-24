#!/usr/bin/env python3
"""
유튜브 쇼츠 자동 편집기 v3
- 이미지 + TTS + 대본 + 틀(프레임) → 세로 쇼츠 영상 자동 생성
- Whisper 기반 정확한 자막 싱크
- CLIP 기반 자동 이미지-텍스트 매칭 (한국어 지원)
- OpenCV 얼굴 감지 크롭
"""

import os
import re
import json
import glob
import subprocess
import unicodedata
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import cv2
import numpy as np

# ============================================================
# 설정
# ============================================================
BASE_DIR = Path(__file__).parent / "original_source"
OUTPUT_DIR = Path(__file__).parent / "output"
TEMP_DIR = Path(__file__).parent / "temp"

# 영상 설정
WIDTH = 1080
HEIGHT = 1920
FPS = 24

# 레이아웃 좌표 (레퍼런스 영상 픽셀 분석 기반)
TITLE_Y = 240                    # 제목 Y 위치
IMAGE_AREA = (37, 472, 1025, 1478)  # 이미지 영역 - 틀의 투명영역
SUBTITLE_Y = 1560                # 자막 Y 위치 (레퍼런스: y=1575~1634)
SUBTITLE_X_MARGIN = 40           # 자막 좌우 마진

# 폰트 설정
FONT_PATH = "/System/Library/Fonts/AppleSDGothicNeo.ttc"
TITLE_FONT_SIZE = 68
TITLE_FONT_INDEX = 7             # Bold (자막과 같은 폰트 패밀리)
SUBTITLE_FONT_SIZE = 42
SUBTITLE_FONT_INDEX = 5          # SemiBold

# ============================================================
# 채널별 설정 (자막/헤드라인 색상)
# ============================================================
CHANNEL_CONFIG = {
    "1.핫찌":     {"subtitle_color": (0, 0, 0),       "headline_color": (0, 0, 0)},
    "2.뉴썰":     {"subtitle_color": (0, 0, 0),       "headline_color": (255, 255, 255)},
    "3.이슈킥":   {"subtitle_color": (255, 255, 255), "headline_color": (255, 255, 255), "subtitle_stroke": (0, 0, 0)},
    "4.찌라핫":   {"subtitle_color": (255, 255, 255), "headline_color": (0, 0, 0),       "subtitle_stroke": (0, 0, 0)},
    "5.핫팩트":   {"subtitle_color": (0, 0, 0),       "headline_color": (0, 0, 0)},
    "6.핫이슈랩": {"subtitle_color": (0, 0, 0),       "headline_color": (255, 255, 255)},
    "7.가쉽온":   {"subtitle_color": (0, 0, 0),       "headline_color": (255, 140, 0)},
    "8.팩톡":     {"subtitle_color": (0, 0, 0),       "headline_color": (0, 0, 0)},
}

# ============================================================
# OpenCV 얼굴 감지
# ============================================================
_face_cascade = None
_face_y_cache = {}


def _get_face_cascade():
    """Haar Cascade 로드 (싱글톤)"""
    global _face_cascade
    if _face_cascade is None:
        _face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
        )
    return _face_cascade


def detect_face_y_ratio(img):
    """
    PIL 이미지에서 가장 큰 얼굴의 Y 중심 비율 (0.0~1.0) 반환
    얼굴 미감지 시 None 반환
    """
    cache_key = id(img)
    if cache_key in _face_y_cache:
        return _face_y_cache[cache_key]

    img_np = np.array(img)
    h, w = img_np.shape[:2]

    # 성능: 최대 640px로 축소하여 감지
    scale = min(1.0, 640 / max(h, w))
    if scale < 1.0:
        small = cv2.resize(img_np, (int(w * scale), int(h * scale)))
    else:
        small = img_np
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)

    cascade = _get_face_cascade()
    faces = cascade.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
    )

    if len(faces) > 0:
        largest = max(faces, key=lambda f: f[2] * f[3])
        fx, fy, fw, fh = largest
        ratio = (fy + fh / 2) / small.shape[0]
        _face_y_cache[cache_key] = ratio
        return ratio

    _face_y_cache[cache_key] = None
    return None


# ============================================================
# CLIP 멀티링구얼 모델 (이미지-텍스트 자동 매칭)
# ============================================================
_clip_model = None
_clip_preprocess = None
_clip_tokenizer = None


def init_clip():
    """멀티링구얼 CLIP 모델 초기화 (한국어 지원)"""
    global _clip_model, _clip_preprocess, _clip_tokenizer
    try:
        import open_clip
        import torch
        print("  🤖 CLIP 모델 로드 중 (xlm-roberta multilingual)...")
        _clip_model, _, _clip_preprocess = open_clip.create_model_and_transforms(
            'xlm-roberta-base-ViT-B-32', pretrained='laion5b_s13b_b90k'
        )
        _clip_tokenizer = open_clip.get_tokenizer('xlm-roberta-base-ViT-B-32')
        _clip_model.eval()
        print("  ✅ CLIP 모델 로드 완료")
        return True
    except Exception as e:
        print(f"  ⚠️ CLIP 로드 실패: {e}")
        return False


def compute_clip_embeddings(images):
    """모든 이미지의 CLIP 임베딩 사전 계산"""
    import torch
    feats = []
    with torch.no_grad():
        for img_data in images:
            tensor = _clip_preprocess(img_data["image"]).unsqueeze(0)
            feat = _clip_model.encode_image(tensor)
            feat /= feat.norm(dim=-1, keepdim=True)
            feats.append(feat)
    return torch.cat(feats)


def load_font(path, size, index=0):
    """폰트 로드"""
    try:
        return ImageFont.truetype(path, size, index=index)
    except Exception:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            print(f"  ⚠️ 폰트 로드 실패, 기본 폰트 사용")
            return ImageFont.load_default()


def parse_script(script_path):
    """개별 대본 파일 파싱 → 헤드라인(멀티라인) + 전체 대본 텍스트"""
    with open(script_path, "r", encoding="utf-8") as f:
        text = f.read()

    # 헤드라인: [헤드라인] ~ [대본] 사이의 모든 줄 캡처
    headline_match = re.search(r"\[헤드라인\]\s*\n([\s\S]+?)(?=\n\[|\Z)", text)
    headline = headline_match.group(1).strip() if headline_match else "뉴스 속보"

    script_match = re.search(r"\[대본\]\s*\n(.+)", text, re.DOTALL)
    script_text = script_match.group(1).strip() if script_match else ""

    return headline, script_text


def parse_script_all(script_path):
    """
    통합 대본 파일 파싱 → {채널명: (헤드라인, 대본)} 딕셔너리 반환
    형식:
      [1.핫찌]
      [헤드라인]
      헤드라인 텍스트
      [대본]
      대본 텍스트

      [2.뉴썰]
      ...
    """
    with open(script_path, "r", encoding="utf-8") as f:
        text = f.read()

    # 채널 블록 분리: [숫자.채널명] 패턴으로 분할
    blocks = re.split(r'\n*\[(\d+\.[^\]]+)\]\s*\n', text)
    # blocks[0]은 첫 매치 이전 텍스트(보통 빈 문자열), 이후 (채널명, 내용) 쌍
    results = {}
    for i in range(1, len(blocks), 2):
        ch_name = blocks[i].strip()
        ch_content = blocks[i + 1] if i + 1 < len(blocks) else ""

        headline_match = re.search(r"\[헤드라인\]\s*\n([\s\S]+?)(?=\n\[|\Z)", ch_content)
        headline = headline_match.group(1).strip() if headline_match else "뉴스 속보"

        script_match = re.search(r"\[대본\]\s*\n(.+)", ch_content, re.DOTALL)
        script_text = script_match.group(1).strip() if script_match else ""

        results[ch_name] = (headline, script_text)

    return results


def get_audio_duration(audio_path):
    """오디오 파일 길이(초) 반환"""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", str(audio_path)],
        capture_output=True, text=True
    )
    info = json.loads(result.stdout)
    return float(info["format"]["duration"])


def split_tts_by_silence(audio_path, n_channels):
    """
    TTS 파일을 묵음 구간(~5초) 기준으로 채널별 오디오로 분할
    → 각 채널의 WAV 파일 경로 리스트 반환
    """
    print(f"  🔊 묵음 감지 중...")

    # 1. FFmpeg silencedetect (3초 이상 묵음 감지)
    cmd = [
        "ffmpeg", "-i", str(audio_path),
        "-af", "silencedetect=noise=-30dB:d=3",
        "-f", "null", "-"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)

    # 2. silence_start / silence_end 파싱
    silence_starts = []
    silence_ends = []
    for line in result.stderr.split('\n'):
        m = re.search(r'silence_start:\s*([\d.]+)', line)
        if m:
            silence_starts.append(float(m.group(1)))
        m = re.search(r'silence_end:\s*([\d.]+)', line)
        if m:
            silence_ends.append(float(m.group(1)))

    total_dur = get_audio_duration(audio_path)

    # 3. 세그먼트 구간 계산 (묵음 사이의 음성 구간)
    segments = []
    if not silence_starts:
        segments = [(0.0, total_dur)]
    else:
        # 첫 세그먼트: 0 ~ 첫 묵음 시작
        if silence_starts[0] > 1.0:
            segments.append((0.0, silence_starts[0]))
        # 중간 세그먼트: 묵음 끝 ~ 다음 묵음 시작
        for i in range(len(silence_ends)):
            seg_start = silence_ends[i]
            seg_end = silence_starts[i + 1] if i + 1 < len(silence_starts) else total_dur
            if seg_end - seg_start > 1.0:  # 1초 미만 세그먼트 필터링
                segments.append((seg_start, seg_end))

    print(f"  🔍 묵음 {len(silence_starts)}개 감지 → {len(segments)}개 세그먼트")

    # 4. 각 세그먼트를 WAV로 추출
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    audio_paths = []
    for i, (start, end) in enumerate(segments[:n_channels]):
        out_path = TEMP_DIR / f"tts_ch{i+1}.wav"
        cmd = [
            "ffmpeg", "-y", "-i", str(audio_path),
            "-ss", str(start), "-to", str(end),
            "-c:a", "pcm_s16le", "-ar", "16000",
            str(out_path)
        ]
        subprocess.run(cmd, capture_output=True)
        dur = get_audio_duration(out_path)
        print(f"    채널{i+1}: {start:.1f}s ~ {end:.1f}s ({dur:.1f}초)")
        audio_paths.append(out_path)

    return audio_paths


def cleanup_temp_frames():
    """프레임 임시 파일만 정리 (TTS 세그먼트는 유지)"""
    for f in TEMP_DIR.glob("frame_*.jpg"):
        f.unlink()


# ============================================================
# Whisper 워드 → 원본 대본 직접 매핑 (정확한 싱크)
# ============================================================
def get_whisper_timings(audio_path, script_text):
    """
    1. Whisper로 워드 단위 타임스탬프 추출
    2. 원본 대본 단어에 1:1 매핑 (Whisper 인식 텍스트 버리고 원본 사용)
    3. 단어들을 ~22자 자막 세그먼트로 그룹핑
    4. 세그먼트 간 갭 제거 (이전 세그먼트 end = 다음 세그먼트 start)
    """
    print("  🔊 Whisper 음성 인식 중...")

    # Whisper 워드 타임스탬프 추출
    whisper_words = _extract_whisper_words(audio_path)
    if not whisper_words:
        print("  ⚠️ Whisper 실패 → 글자수 비례 타이밍 대체")
        return _fallback_timings(script_text, get_audio_duration(audio_path))

    # 원본 대본 단어 분리
    original_words = script_text.split()
    print(f"  Whisper 워드: {len(whisper_words)}개 / 원본 워드: {len(original_words)}개")

    # 원본 단어에 Whisper 타임스탬프 매핑
    timed_words = _map_words(original_words, whisper_words)

    # 단어들을 짧은 자막 세그먼트로 그룹핑 (한줄 보장)
    segments = _group_words_to_segments(timed_words, max_chars=22)

    print(f"  ✅ {len(segments)}개 세그먼트 (원본 타이밍 유지)")
    return segments


def _extract_whisper_words(audio_path):
    """Whisper에서 워드 단위 타임스탬프 추출"""
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("base", device="cpu", compute_type="int8")
        segments_iter, _ = model.transcribe(
            str(audio_path), language="ko", word_timestamps=True
        )
        words = []
        for seg in segments_iter:
            for w in seg.words:
                words.append({"start": w.start, "end": w.end, "word": w.word.strip()})
        return words
    except Exception as e:
        print(f"  ⚠️ Whisper 에러: {e}")
        return None


def _map_words(original_words, whisper_words):
    """
    순차 소비 방식 매핑 (non-overlapping 보장)
    - 원본/Whisper 글자를 스트림으로 취급
    - 원본 단어의 글자 수만큼 Whisper 글자를 순차적으로 소비
    - 글자 비율 스케일링으로 총 길이 차이 보정
    - 하나의 Whisper 워드 내에서 부분 사용 시 시간 보간
    """
    o_total = sum(len(w) for w in original_words)
    w_total = sum(len(w["word"]) for w in whisper_words)
    scale = w_total / o_total if o_total > 0 else 1.0

    timed = []
    w_ptr = 0           # 현재 Whisper 워드 인덱스
    w_char_used = 0.0   # 현재 Whisper 워드에서 소비한 글자 수

    for orig_word in original_words:
        chars_to_consume = len(orig_word) * scale
        chars_remaining = chars_to_consume

        start_time = None
        end_time = None

        while chars_remaining > 0.001 and w_ptr < len(whisper_words):
            ww = whisper_words[w_ptr]
            w_len = len(ww["word"])
            w_chars_left = w_len - w_char_used

            # 시작 시간: 현재 Whisper 워드 내 소비 위치에서 보간
            if start_time is None:
                w_dur = ww["end"] - ww["start"]
                frac = w_char_used / w_len if w_len > 0 else 0
                start_time = ww["start"] + w_dur * frac

            if chars_remaining >= w_chars_left - 0.001:
                # 이 Whisper 워드 전부 소비 → 다음 워드로
                end_time = ww["end"]
                chars_remaining -= w_chars_left
                w_ptr += 1
                w_char_used = 0.0
            else:
                # Whisper 워드 일부만 소비 → 시간 보간
                w_dur = ww["end"] - ww["start"]
                frac = (w_char_used + chars_remaining) / w_len
                end_time = ww["start"] + w_dur * frac
                w_char_used += chars_remaining
                chars_remaining = 0

        # 남은 Whisper 워드가 없으면 마지막 시간 사용
        if start_time is None:
            start_time = whisper_words[-1]["end"]
        if end_time is None:
            end_time = whisper_words[-1]["end"]

        timed.append({
            "word": orig_word,
            "start": round(start_time, 3),
            "end": round(end_time, 3)
        })

    return timed


def _group_words_to_segments(timed_words, max_chars=22):
    """
    타이밍이 있는 단어들을 max_chars 이내의 자막 세그먼트로 그룹핑
    구두점(. ? ! ,)이 있으면 거기서 끊기
    """
    segments = []
    current_words = []
    current_text = ""

    for tw in timed_words:
        test_text = (current_text + " " + tw["word"]).strip()

        # 현재 세그먼트에 추가할지 / 새 세그먼트 시작할지
        should_break = False
        if len(test_text) > max_chars and current_words:
            should_break = True
        elif current_text and current_text[-1] in '.?!':
            should_break = True

        if should_break:
            segments.append({
                "text": current_text,
                "start": current_words[0]["start"],
                "end": current_words[-1]["end"]
            })
            current_words = [tw]
            current_text = tw["word"]
        else:
            current_words.append(tw)
            current_text = test_text

    # 마지막 세그먼트
    if current_words:
        segments.append({
            "text": current_text,
            "start": current_words[0]["start"],
            "end": current_words[-1]["end"]
        })

    return segments


def _remove_gaps(segments):
    """
    세그먼트 간 갭 제거 → 이전 세그먼트의 end를 다음 세그먼트 start로 설정
    자막이 끊기지 않고 연속으로 이어짐
    """
    for i in range(len(segments)):
        if i == 0:
            segments[i]["start"] = 0.0  # 영상 시작부터 첫 자막 표시
        else:
            segments[i]["start"] = segments[i - 1]["end"]
        segments[i]["duration"] = segments[i]["end"] - segments[i]["start"]
    return segments


def _fallback_timings(script_text, total_duration):
    """Whisper 실패 시 글자수 비례 타이밍"""
    words = script_text.split()
    timed = []
    total_chars = len(script_text.replace(" ", ""))
    current = 0.0
    for w in words:
        dur = total_duration * (len(w) / total_chars)
        timed.append({"word": w, "start": current, "end": current + dur})
        current += dur
    return _remove_gaps(_group_words_to_segments(timed, max_chars=22))


# ============================================================
# 이미지 처리
# ============================================================
def load_images(image_dir):
    """이미지 폴더에서 순서대로 이미지 로드 + 얼굴 감지"""
    extensions = ['*.jpg', '*.jpeg', '*.png', '*.webp', '*.avif']
    image_files = []
    for ext in extensions:
        image_files.extend(glob.glob(os.path.join(image_dir, ext)))

    def sort_key(path):
        name = Path(path).stem
        try:
            return int(name)
        except ValueError:
            nums = re.findall(r'\d+', name)
            return int(nums[-1]) if nums else float('inf')

    image_files.sort(key=sort_key)
    images = []
    for img_path in image_files:
        try:
            img = Image.open(img_path).convert("RGB")
            face_y = detect_face_y_ratio(img)
            face_str = f"👤 얼굴 Y={face_y:.0%}" if face_y is not None else "얼굴 미감지"
            images.append({"path": img_path, "image": img, "face_y": face_y})
            print(f"  ✅ {Path(img_path).name} ({img.size[0]}x{img.size[1]}, {face_str})")
        except Exception as e:
            print(f"  ⚠️ 로드 실패: {Path(img_path).name} - {e}")
    return images


ZOOM_MAX = 1.12  # 줌인 최대 비율 (1.0 → 1.12 = 12% 확대)

# 이미지 pre-resize 캐시 (줌 애니메이션 시 흔들림 방지)
_resize_cache = {}


def fit_image_to_area(img, area, face_y_ratio=None, zoom=1.0):
    """
    이미지를 영역에 맞게 크롭 + 리사이즈 (흔들림 없는 줌)
    1) 최대 줌 크기로 한 번만 resize (캐시)
    2) float 좌표 box= 파라미터로 서브픽셀 정밀도 → 흔들림 완전 제거
    face_y_ratio: OpenCV 감지된 얼굴 Y 중심 비율 (0.0~1.0), None이면 상단 1/3 휴리스틱
    """
    x1, y1, x2, y2 = area
    target_w = x2 - x1
    target_h = y2 - y1

    # ── 1. 최대 줌 크기로 한 번만 resize (캐시) ──
    cache_key = id(img)
    if cache_key not in _resize_cache:
        img_w, img_h = img.size
        base_scale = max(target_w / img_w, target_h / img_h) * ZOOM_MAX
        rw = int(img_w * base_scale)
        rh = int(img_h * base_scale)
        _resize_cache[cache_key] = img.resize((rw, rh), Image.LANCZOS)

    img_resized = _resize_cache[cache_key]
    rw, rh = img_resized.size

    # ── 2. float 좌표로 crop 영역 계산 (정수 반올림 없음 = 흔들림 없음) ──
    crop_w = target_w * ZOOM_MAX / zoom
    crop_h = target_h * ZOOM_MAX / zoom
    crop_w = min(crop_w, float(rw))
    crop_h = min(crop_h, float(rh))

    # X축: 중앙
    left = (rw - crop_w) / 2.0

    # Y축: 얼굴 감지 기반 크롭
    if rh > crop_h:
        if face_y_ratio is not None:
            # 실제 얼굴 위치 중심 크롭
            face_y_px = face_y_ratio * rh
            top = face_y_px - crop_h / 3.0
        else:
            # 얼굴 미감지 → 상단 1/3 휴리스틱
            top = rh / 3.0 - crop_h / 3.0
        top = max(0.0, top)
        top = min(top, rh - crop_h)
    else:
        top = (rh - crop_h) / 2.0

    # box=(float, float, float, float) → 서브픽셀 보간, 흔들림 완전 제거
    return img_resized.resize(
        (target_w, target_h), Image.LANCZOS,
        box=(left, top, left + crop_w, top + crop_h)
    )


# ============================================================
# CLIP 기반 이미지 매칭 (최소 2초~최대 4초, 중복 없음)
# ============================================================
MIN_IMAGE_DURATION = 2.0  # 이미지 최소 유지 시간(초)
MAX_IMAGE_DURATION = 4.0  # 이미지 최대 유지 시간(초)


def _group_segments_to_blocks(timings):
    """세그먼트들을 2~4초 블록으로 그룹핑"""
    n_segments = len(timings)
    blocks = []
    block_start = 0
    block_start_time = timings[0]["start"]

    for i in range(n_segments):
        block_duration = timings[i]["end"] - block_start_time

        should_close = False
        if block_duration >= MAX_IMAGE_DURATION:
            should_close = True
        elif block_duration >= MIN_IMAGE_DURATION and i < n_segments - 1:
            next_duration = timings[i + 1]["end"] - block_start_time
            if next_duration > MAX_IMAGE_DURATION:
                should_close = True

        if should_close and i < n_segments - 1:
            blocks.append({
                "seg_start": block_start,
                "seg_end": i,
                "texts": [timings[j]["text"] for j in range(block_start, i + 1)]
            })
            block_start = i + 1
            block_start_time = timings[i + 1]["start"]

    blocks.append({
        "seg_start": block_start,
        "seg_end": n_segments - 1,
        "texts": [timings[j]["text"] for j in range(block_start, n_segments)]
    })

    return blocks


def assign_images(timings, images, clip_embeddings=None):
    """
    CLIP 유사도 기반 이미지 매칭 (CLIP 실패 시 순차 배정)
    1단계: 세그먼트 → 2~4초 블록 그룹핑
    2단계: 각 블록 텍스트 vs 이미지 CLIP 유사도 → 최고 매칭 (중복 금지)
    3단계: 블록 → 세그먼트 배열 변환
    """
    import torch
    n_segments = len(timings)

    # ── 1단계: 블록 그룹핑 ──
    blocks = _group_segments_to_blocks(timings)
    print(f"  📦 {len(blocks)}개 이미지 블록 ({MIN_IMAGE_DURATION}~{MAX_IMAGE_DURATION}초)")

    # ── 2단계: 이미지 매칭 ──
    used_images = set()
    block_img = []

    if clip_embeddings is not None and _clip_model is not None:
        # ✅ CLIP 유사도 매칭
        print("  🤖 CLIP 유사도 기반 매칭:")
        with torch.no_grad():
            for blk_idx, block in enumerate(blocks):
                text = " ".join(block["texts"])
                tokens = _clip_tokenizer([text])
                text_feat = _clip_model.encode_text(tokens)
                text_feat /= text_feat.norm(dim=-1, keepdim=True)

                sims = (text_feat @ clip_embeddings.T).squeeze(0)

                # 사용된 이미지 마스킹
                for used in used_images:
                    sims[used] = -float('inf')

                best = sims.argmax().item()

                # 모든 이미지 소진 시 전체에서 최고 유사도 재사용
                if sims[best] == -float('inf'):
                    sims_full = (text_feat @ clip_embeddings.T).squeeze(0)
                    best = sims_full.argmax().item()

                used_images.add(best)
                block_img.append(best)
                img_name = Path(images[best]["path"]).name
                sim_val = sims[best].item() if sims[best] != -float('inf') else 0
                print(f"    블록{blk_idx+1} \"{text[:25]}\" → {img_name} (유사도: {sim_val:.3f})")
    else:
        # ⚠️ 순차 배정 (CLIP 미사용 시)
        print("  ⚠️ CLIP 미사용 → 순차 배정:")
        for blk_idx in range(len(blocks)):
            img_idx = blk_idx % len(images)
            block_img.append(img_idx)
            img_name = Path(images[img_idx]["path"]).name
            print(f"    블록{blk_idx+1} → {img_name} (순차)")

    # ── 3단계: 블록 → 세그먼트별 이미지 배열 ──
    assignments = [0] * n_segments
    for blk_idx, block in enumerate(blocks):
        for seg_idx in range(block["seg_start"], block["seg_end"] + 1):
            assignments[seg_idx] = block_img[blk_idx]

    return assignments


# ============================================================
# 렌더링
# ============================================================
def render_frame(headline, subtitle_text, content_image, frame_overlay,
                 title_font, subtitle_font, zoom=1.0, face_y_ratio=None,
                 headline_color=(0, 0, 0), subtitle_color=(0, 0, 0),
                 subtitle_stroke=None):
    """단일 프레임 렌더링 (채널별 색상 지원)"""
    # 1. 흰색 배경
    canvas = Image.new("RGBA", (WIDTH, HEIGHT), (255, 255, 255, 255))

    # 2. 콘텐츠 이미지 배치 (프레임 아래 레이어, 줌 적용)
    if content_image:
        fitted = fit_image_to_area(content_image, IMAGE_AREA, face_y_ratio=face_y_ratio, zoom=zoom)
        fitted_rgba = fitted.convert("RGBA")
        canvas.paste(fitted_rgba, (IMAGE_AREA[0], IMAGE_AREA[1]))

    # 3. 프레임 오버레이
    canvas = Image.alpha_composite(canvas, frame_overlay)

    # 4. 텍스트는 프레임 위에 그리기
    draw = ImageDraw.Draw(canvas)

    # 5. 제목 텍스트 (중앙 정렬, Bold weight, 멀티라인 지원)
    title_lines = headline.split('\n')
    line_spacing = 14  # 줄 간격
    # 각 줄 높이 측정
    line_heights = []
    for line in title_lines:
        bbox = draw.textbbox((0, 0), line, font=title_font)
        line_heights.append(bbox[3] - bbox[1])
    total_title_h = sum(line_heights) + line_spacing * (len(title_lines) - 1)
    # 제목 영역 수직 중앙 정렬 (TITLE_Y 기준)
    if len(title_lines) == 1:
        y_start = TITLE_Y
    else:
        y_start = TITLE_Y - (total_title_h - line_heights[0]) // 2
    for i, line in enumerate(title_lines):
        bbox = draw.textbbox((0, 0), line, font=title_font)
        line_w = bbox[2] - bbox[0]
        line_x = (WIDTH - line_w) // 2
        draw.text((line_x, y_start), line, fill=headline_color, font=title_font)
        y_start += line_heights[i] + line_spacing

    # 6. 자막 텍스트 (무조건 한 줄, 중앙 정렬)
    if subtitle_text:
        sub_bbox = draw.textbbox((0, 0), subtitle_text, font=subtitle_font)
        sub_w = sub_bbox[2] - sub_bbox[0]
        sub_h = sub_bbox[3] - sub_bbox[1]
        sub_x = (WIDTH - sub_w) // 2
        stroke_kwargs = {}
        if subtitle_stroke:
            stroke_kwargs = {"stroke_width": 3, "stroke_fill": subtitle_stroke}
        draw.text((sub_x, SUBTITLE_Y), subtitle_text,
                  fill=subtitle_color, font=subtitle_font, **stroke_kwargs)

    return canvas.convert("RGB")


def generate_frames(headline, timings, images, image_assignments,
                    frame_overlay, title_font, subtitle_font,
                    audio_duration=None,
                    headline_color=(0, 0, 0), subtitle_color=(0, 0, 0),
                    subtitle_stroke=None):
    """모든 프레임 생성 (채널별 색상 지원)"""
    total_duration = max(timings[-1]["end"], audio_duration or 0)
    total_frames = int(total_duration * FPS)

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n🎬 총 {total_frames}프레임 생성 ({total_duration:.1f}초, {FPS}fps)")

    current_segment = 0
    prev_subtitle = timings[0]["text"]

    # 줌인 효과: 이미지 블록 시작/끝 프레임 추적
    prev_img_idx = -1
    block_start_frame = 0
    block_total_frames = 1  # 현재 블록의 총 프레임 수

    for frame_idx in range(total_frames):
        current_time = frame_idx / FPS

        # 현재 시간에 해당하는 세그먼트 찾기
        while (current_segment < len(timings) - 1 and
               current_time >= timings[current_segment]["end"]):
            prev_subtitle = timings[current_segment]["text"]
            current_segment += 1

        # 세그먼트 발화 구간이면 해당 자막, 갭이면 이전 자막 유지
        if current_time >= timings[current_segment]["start"]:
            subtitle_text = timings[current_segment]["text"]
            prev_subtitle = subtitle_text
        else:
            subtitle_text = prev_subtitle

        img_idx = image_assignments[current_segment]
        content_image = images[img_idx]["image"]
        face_y = images[img_idx].get("face_y")

        # 이미지 변경 감지 → 줌 리셋 + 블록 끝 계산
        if img_idx != prev_img_idx:
            block_start_frame = frame_idx
            prev_img_idx = img_idx
            # 이 이미지 블록이 끝나는 프레임 찾기
            block_end_frame = total_frames
            for future_seg in range(current_segment + 1, len(image_assignments)):
                if image_assignments[future_seg] != img_idx:
                    block_end_frame = int(timings[future_seg]["start"] * FPS)
                    break
            block_total_frames = max(block_end_frame - block_start_frame, 1)

        # 줌 진행도: 블록 시작(0.0) → 블록 끝(1.0) 정확히 맞춤
        frames_in_block = frame_idx - block_start_frame
        zoom_progress = min(frames_in_block / block_total_frames, 1.0)
        zoom = 1.0 + zoom_progress * (ZOOM_MAX - 1.0)

        frame = render_frame(
            headline, subtitle_text, content_image,
            frame_overlay, title_font, subtitle_font,
            zoom=zoom, face_y_ratio=face_y,
            headline_color=headline_color, subtitle_color=subtitle_color,
            subtitle_stroke=subtitle_stroke
        )

        frame_path = TEMP_DIR / f"frame_{frame_idx:06d}.jpg"
        frame.save(str(frame_path), quality=95)

        if frame_idx % (FPS * 2) == 0:
            progress = (frame_idx / total_frames) * 100
            print(f"  📸 {progress:.0f}% - [{current_segment+1}/{len(timings)}] "
                  f"{subtitle_text[:20]}...")

    print(f"  ✅ 프레임 생성 완료: {total_frames}장")
    return total_frames


def assemble_video(total_frames, audio_path, output_path):
    """FFmpeg 영상 합성"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-framerate", str(FPS),
        "-i", str(TEMP_DIR / "frame_%06d.jpg"),
        "-i", str(audio_path),
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(output_path)
    ]
    print(f"\n🎥 영상 합성 중...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"  ✅ 완료: {output_path} ({size_mb:.1f}MB)")
    else:
        print(f"  ❌ FFmpeg 에러:\n{result.stderr[:500]}")
    return result.returncode == 0


def cleanup_temp():
    import shutil
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR)
        print("🧹 임시 파일 정리 완료")


# ============================================================
# 메인 (8채널 일괄 처리)
# ============================================================
def main():
    print("=" * 60)
    print("🎬 유튜브 쇼츠 자동 편집기 v3 (멀티채널)")
    print("=" * 60)

    # ── 1. 채널 폴더 스캔 ──
    channel_base = BASE_DIR / "Channel"
    channel_dirs = sorted([
        d for d in channel_base.iterdir()
        if d.is_dir() and not d.name.startswith('.')
    ])
    n_channels = len(channel_dirs)
    print(f"\n📺 {n_channels}개 채널 감지:")
    for d in channel_dirs:
        print(f"  - {d.name}")

    # ── 2. 공유 리소스: 이미지 + 얼굴 감지 ──
    print("\n🖼️ 이미지 로드 + 얼굴 감지...")
    images = load_images(BASE_DIR / "image")
    face_count = sum(1 for img in images if img.get("face_y") is not None)
    print(f"  총 {len(images)}장 (얼굴 감지: {face_count}장)")

    # ── 3. 공유 리소스: CLIP 모델 + 임베딩 ──
    print("\n🤖 CLIP 모델 초기화...")
    clip_ok = init_clip()
    clip_embeddings = None
    if clip_ok:
        print("  📊 이미지 임베딩 계산 중...")
        clip_embeddings = compute_clip_embeddings(images)
        print(f"  ✅ {len(images)}장 임베딩 완료")

    # ── 4. 공유 리소스: 폰트 ──
    title_font = load_font(FONT_PATH, TITLE_FONT_SIZE, index=TITLE_FONT_INDEX)
    subtitle_font = load_font(FONT_PATH, SUBTITLE_FONT_SIZE, index=SUBTITLE_FONT_INDEX)

    # ── 5. 대본 로드 (통합 대본 우선) ──
    all_scripts = None
    all_script_path = BASE_DIR / "대본_전체.txt"
    if all_script_path.exists():
        all_scripts = parse_script_all(all_script_path)
        print(f"\n📄 통합 대본 로드: {len(all_scripts)}개 채널")
        for ch, (hl, sc) in all_scripts.items():
            print(f"  - {ch}: \"{hl.split(chr(10))[0]}\" ({len(sc)}자)")
    else:
        print(f"\n📄 통합 대본 없음 → 개별 대본.txt 사용")

    # ── 5b. TTS 묵음 분할 ──
    print("\n✂️ TTS 묵음 분할...")
    tts_path = BASE_DIR / "TTS.mp3"
    audio_segments = split_tts_by_silence(tts_path, n_channels)

    if len(audio_segments) < n_channels:
        print(f"  ⚠️ TTS 세그먼트({len(audio_segments)}개) < 채널({n_channels}개)")
        n_channels = len(audio_segments)

    # ── 6. 채널별 영상 생성 ──
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    success_count = 0

    for ch_idx in range(n_channels):
        ch_dir = channel_dirs[ch_idx]
        ch_name = ch_dir.name
        # macOS NFD 한글 → NFC 정규화 (CHANNEL_CONFIG 키 매칭)
        ch_name_nfc = unicodedata.normalize('NFC', ch_name)
        config = CHANNEL_CONFIG.get(ch_name_nfc, {
            "subtitle_color": (0, 0, 0),
            "headline_color": (0, 0, 0)
        })

        print(f"\n{'=' * 60}")
        print(f"📺 [{ch_idx+1}/{n_channels}] {ch_name}")
        print(f"  🎨 헤드라인 색상: {config['headline_color']}")
        print(f"  🎨 자막 색상: {config['subtitle_color']}")
        print(f"{'=' * 60}")

        # 6-1. 대본 파싱 (통합 대본 우선, 없으면 개별 대본)
        if all_scripts and ch_name_nfc in all_scripts:
            headline, script_text = all_scripts[ch_name_nfc]
        elif (ch_dir / "대본.txt").exists():
            headline, script_text = parse_script(ch_dir / "대본.txt")
        else:
            print(f"  ⚠️ 대본 없음 → 건너뜀")
            continue
        print(f"  📝 헤드라인: {headline.replace(chr(10), ' / ')}")
        print(f"  📝 대본: {len(script_text)}자")

        # 6-2. Whisper 타이밍
        audio_path = audio_segments[ch_idx]
        print(f"  🎙️ Whisper 싱크...")
        timings = get_whisper_timings(audio_path, script_text)

        # 6-3. CLIP 이미지 매칭
        print(f"  🔗 이미지 매칭...")
        image_assignments = assign_images(timings, images, clip_embeddings)

        # 6-4. 프레임 오버레이 (채널별)
        frame_overlay = Image.open(ch_dir / "틀.png").convert("RGBA")
        if frame_overlay.size != (WIDTH, HEIGHT):
            frame_overlay = frame_overlay.resize((WIDTH, HEIGHT), Image.LANCZOS)

        # 6-5. 프레임 생성 (채널별 색상 적용)
        audio_duration = get_audio_duration(audio_path)
        total_frames = generate_frames(
            headline, timings, images, image_assignments,
            frame_overlay, title_font, subtitle_font,
            audio_duration=audio_duration,
            headline_color=config["headline_color"],
            subtitle_color=config["subtitle_color"],
            subtitle_stroke=config.get("subtitle_stroke")
        )

        # 6-6. 영상 합성
        output_path = OUTPUT_DIR / f"{ch_name}_shorts.mp4"
        success = assemble_video(total_frames, audio_path, output_path)

        if success:
            success_count += 1
            cleanup_temp_frames()  # 프레임만 정리, TTS 세그먼트 유지

    # ── 7. 최종 정리 ──
    cleanup_temp()
    print(f"\n{'=' * 60}")
    print(f"🎉 완성! {success_count}/{n_channels}채널 영상 생성")
    print(f"📁 출력: {OUTPUT_DIR}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()

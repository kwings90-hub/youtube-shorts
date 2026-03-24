#!/usr/bin/env python3
"""
연예 쇼츠 8채널 대본 생성기
기사를 입력하면 8개 채널 페르소나에 맞는 헤드라인 + TTS 대본을 생성

LLM 독립적 구조 — call_llm() 함수만 교체하면 어떤 LLM이든 사용 가능
"""

import re
import time

# ============================================================
# 8개 채널 정의
# ============================================================
CHANNELS = {
    "1.핫찌": {
        "opening": "긴급 속보형",
        "persona": "15년 차 연예부 리포터. '단독 보도' 느낌으로 팩트 전달. 톤스온 배제하고 소스의 공식 입장과 팩트 사이 행간 읽기. 팩트 중심이지만 딱딱하지 않게 비유와 위트 섞기",
        "tone": "신뢰감, 위트, 밝음",
        "headline_style": "[단독], [속보], '확인됐다' 등 뉴스 전문성 + 팩트 강조",
    },
    "2.뉴썰": {
        "opening": "비밀 폭로형",
        "persona": "어그로가 생명. 자극적인 헤드라인 만들고 자극적인 단어로 시작. 텐션 높고 과장된 리액션",
        "tone": "자극적, 과장됨, 높은 텐션",
        "headline_style": "[충격], [경악], '미쳤다' 등 극도의 자극",
    },
    "3.이슈킥": {
        "opening": "충격 칼럼형",
        "persona": "'할 미친 대박'을 입에 달고 삼. 감정 표현 풍부하고 느낌표(!!!) 많이 씀. 과몰입하며 트렌디한 인터넷 용어 자연스럽게 섞음",
        "tone": "호들갑, 감정 과잉, 인터넷 용어",
        "headline_style": "'대박', '왜 지금?' 등 의문/궁금증 중심",
    },
    "4.찌라핫": {
        "opening": "충격 칼럼형",
        "persona": "긴 뉴스는 딱 전생. 서론~~본론~~결론을 빠르게 말함. 음슴체로 핵심만 딱딱 짚음. 35초 분량 지키면서 속도감 살림",
        "tone": "간결함, 음슴체, 속도감",
        "headline_style": "'역대급이네' 등 행동 중심",
    },
    "5.핫팩트": {
        "opening": "충격 칼럼형",
        "persona": "바쁜 현대인을 위해 뉴스를 반말로 ~잉, ~슴 체 사용. 서론 없이 바로 본론. 군더더기 빼고 핵심(누가, 왜, 그래서 어떻게 됨)만 속도감 있게 전달",
        "tone": "빠름, 간결함, 핵심 전달",
        "headline_style": "'3줄 요약', '결국 터졌다' 등 빠른 정보 전달 + 반말",
    },
    "6.핫이슈랩": {
        "opening": "대조/반전형",
        "persona": "커뮤니티(디큐, 인스티즈 등) 베스트 글과 댓글. '지금 난리 난 그 사건 정리해 줌^ㅁ^' 이러면 반말(음슴체)로 써줌. 네티즌들의 베스트 댓글 반응 모아서 전달",
        "tone": "반말(음슴체), 트렌디, 전달자",
        "headline_style": "'난리남', '댓글 폭발' 등 네티즌 반응 중심",
    },
    "7.가쉽온": {
        "opening": "비밀 폭로형",
        "persona": "헤드라인 제목 장인. 시작부터 어그로 끌기. 별거 아닌 일도 세상 망할 것처럼 포장",
        "tone": "자극적, 선동적, 다급함",
        "headline_style": "'발칵', '충격적 이유', '진실은?' 등 선동적 + 궁금증 유발",
    },
    "8.팩톡": {
        "opening": "의문 제기형",
        "persona": "연예계 소식을 가장 재치 있고 웃기게 전달하는 예능 작가. 자칫 무거울 수 있는 가십도 각종 밈(Meme)과 드립을 섞어 가볍게 풀어냄. 웃음 나오는 비유와 위트 있는 표현을 문장마다 넣음. 시청자가 영상 보며 피식 웃게 만드는 게 최우선",
        "tone": "유머, 해학, 빠름",
        "headline_style": "'실화냐', '역대급' 등 유머러스 + 비꼼",
    },
}


# ============================================================
# LLM 호출 (이 함수만 교체하면 됨)
# ============================================================
def call_llm(prompt):
    """
    LLM API 호출 — 사용할 LLM에 맞게 이 함수만 교체

    예시 (Anthropic Claude):
        import anthropic
        client = anthropic.Anthropic(api_key="sk-ant-...")
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in response.content if b.type == "text")

    예시 (OpenAI GPT):
        from openai import OpenAI
        client = OpenAI(api_key="sk-...")
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content

    예시 (Google Gemini):
        import google.generativeai as genai
        genai.configure(api_key="...")
        model = genai.GenerativeModel("gemini-1.5-pro")
        response = model.generate_content(prompt)
        return response.text

    Returns:
        str: LLM 응답 텍스트
    """
    raise NotImplementedError(
        "call_llm()에 사용할 LLM API를 연결하세요. 위 docstring 예시 참고."
    )


# ============================================================
# 프롬프트 생성
# ============================================================
def build_prompt(channel_name, channel_info, article, length=30):
    """채널별 API 프롬프트 생성"""
    return f"""당신은 연예 뉴스 쇼츠 스크립트 작가입니다.

# 채널 정보
- 채널명: {channel_name}
- 페르소나: {channel_info['persona']}
- 톤: {channel_info['tone']}
- 오프닝 타입: {channel_info['opening']}
- 헤드라인 스타일: {channel_info['headline_style']}

# 입력 기사
{article}

# 지시사항
위 연예기사를 {channel_name} 채널의 페르소나에 맞게 다음 2가지를 생성하세요:

1. **썸네일/헤드라인 자막** (두 줄 형식)
   - 영상 썸네일에 들어갈 강력한 두 줄 자막
   - 공백 포함 최대 24자
   - 2줄의 글자 수가 1줄보다 많거나 비슷하게

2. **TTS 스크립트** ({length}초 분량)
   - ㅋㅋㅋ/ㅠㅠ 같은 자음모음 표현 금지
   - 페르소나 스타일 정확히 반영
   - 한 문단으로 자연스럽게

출력 형식:
HEADLINE: [1줄 자막]
[2줄 자막]
SCRIPT: [스크립트]"""


# ============================================================
# 응답 파싱
# ============================================================
def parse_response(text):
    """HEADLINE과 SCRIPT 파싱"""
    headline = ""
    script = ""

    headline_match = re.search(r"HEADLINE:\s*(.+?)(?=\nSCRIPT:|\Z)", text, re.DOTALL)
    script_match = re.search(r"SCRIPT:\s*(.+)", text, re.DOTALL)

    if headline_match:
        headline = headline_match.group(1).strip()
    if script_match:
        script = script_match.group(1).strip()

    return headline, script


# ============================================================
# 생성
# ============================================================
def generate_single(channel_name, channel_info, article, length=30):
    """단일 채널 생성"""
    prompt = build_prompt(channel_name, channel_info, article, length)
    text = call_llm(prompt)
    return parse_response(text)


def generate_all(article, length=30, progress_callback=None):
    """
    8개 채널 전체 생성

    Args:
        article: 연예 기사 텍스트
        length: 쇼츠 길이 (초)
        progress_callback: fn(channel_name, headline, script) 진행 콜백

    Returns:
        dict: {채널명: {"headline": ..., "script": ...}, ...}
    """
    results = {}

    for channel_name, channel_info in CHANNELS.items():
        try:
            headline, script = generate_single(
                channel_name, channel_info, article, length
            )
            results[channel_name] = {"headline": headline, "script": script}

            if progress_callback:
                progress_callback(channel_name, headline, script)

            print(f"  ✅ {channel_name} 완료")
        except Exception as e:
            results[channel_name] = {"headline": "오류", "script": str(e)}
            print(f"  ❌ {channel_name} 실패: {e}")

    return results


# ============================================================
# 출력
# ============================================================
def format_output(results):
    """다운로드/복사용 텍스트 포맷"""
    lines = []
    for channel_name, result in results.items():
        lines.append(f"[{channel_name}]")
        lines.append("[헤드라인]")
        lines.append(result["headline"])
        lines.append("[대본]")
        lines.append(result["script"])
        lines.append("")
    return "\n".join(lines)


def save_output(results, output_dir=None):
    """결과를 txt 파일로 저장"""
    from pathlib import Path

    if output_dir is None:
        output_dir = Path(__file__).parent / "output"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"연예쇼츠_8채널_{timestamp}.txt"
    filepath = output_dir / filename

    content = format_output(results)
    filepath.write_text(content, encoding="utf-8")
    print(f"\n💾 저장 완료: {filepath}")
    return filepath


# ============================================================
# 단독 실행 테스트
# ============================================================
if __name__ == "__main__":
    sample_article = input("연예 기사를 입력하세요:\n> ")
    if not sample_article.strip():
        print("기사를 입력해주세요.")
        exit(1)

    print("\n🚀 8개 채널 생성 시작...\n")
    results = generate_all(sample_article, length=30)
    print("\n" + "=" * 50)
    print(format_output(results))
    save_output(results)

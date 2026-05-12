"""Unit tests for _detect_transcript_degradation in gemini_analyzer.

Six tests covering three failure modes, one clean case, and two edge cases.
The function is pure (no I/O, no mocking needed).
"""

from tools.integrations.gemini_analyzer import (
    _detect_transcript_degradation,
    _PROMPT_ECHO_PHRASES,
)


CHUNK_LABEL = "chunk 1/4"


class TestDetectTranscriptDegradation:
    def test_prompt_echo_returns_reason(self):
        """Prompt-echo: output contains a known prompt opening phrase."""
        echo_phrase = _PROMPT_ECHO_PHRASES[0]
        # Pad well past the 50-char minimum so we reach the echo check
        text = "x" * 60 + "\n" + echo_phrase + "\nMore text that continues here."
        result = _detect_transcript_degradation(text, CHUNK_LABEL)
        assert result is not None
        assert "prompt-echo" in result

    def test_srt_timestamp_spiral_returns_reason(self):
        """Timestamp-spiral: >30% of lines are SRT timestamp markers."""
        # 40 SRT lines out of 50 total = 80%, well above 30% threshold
        ts_line = "00:01:23,000 --> 00:01:26,000"
        content_line = "მოდი ვისაუბროთ AI-ზე"
        lines = [ts_line] * 40 + [content_line] * 10
        text = "\n".join(lines)
        result = _detect_transcript_degradation(text, CHUNK_LABEL)
        assert result is not None
        assert "timestamp spiral" in result

    def test_ngram_repetition_returns_reason(self):
        """Repetition loop: an 8-word window repeats more than _MAX_NGRAM_REPEATS times."""
        # Repeat the same 8-word phrase 10 times (> the threshold of 6)
        repeated_phrase = (
            "კლოდი ძალიან კარგი ხელოვნური ინტელექტის სისტემაა ყველა ამოცანისთვის"
        )
        text = (repeated_phrase + " ") * 10
        result = _detect_transcript_degradation(text, CHUNK_LABEL)
        assert result is not None
        assert "repetition loop" in result

    def test_clean_transcript_returns_none(self):
        """Clean Georgian text should pass all checks and return None."""
        text = (
            "გამარჯობა, დღეს ვისწავლით Claude-ის შესახებ. "
            "ეს არის Anthropic-ის მიერ შექმნილი AI ასისტენტი. "
            "მას შეუძლია კოდის დაწერა, ტექსტის ანალიზი, "
            "და სხვა მრავალი სასარგებლო ამოცანის შესრულება. "
            "ლექცია გაგრძელდება ორი საათი. მოდი დავიწყოთ საფუძვლებიდან. "
            "AI-ის ისტორია სათავეს 1950-იანი წლებიდან იღებს."
        )
        result = _detect_transcript_degradation(text, CHUNK_LABEL)
        assert result is None

    def test_empty_string_returns_reason(self):
        """Empty string is too short — degradation reason expected."""
        result = _detect_transcript_degradation("", CHUNK_LABEL)
        assert result is not None

    def test_short_transcript_under_50_chars_returns_reason(self):
        """A transcript under 50 chars is considered too short to be valid."""
        short = "მოკლე ტექსტი"  # well under 50 chars
        assert len(short.strip()) < 50
        result = _detect_transcript_degradation(short, CHUNK_LABEL)
        assert result is not None

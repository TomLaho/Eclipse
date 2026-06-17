"""Tests for eclipse.transcribe.diarize.

All tests run without torch / pyannote installed — they only exercise the pure
``assign_speakers`` function and the writer's speaker-rendering helper.
"""

from __future__ import annotations

from datetime import date

from eclipse.models import MeetingInsights, ProcessedMeeting, Segment, TranscriptResult, Word
from eclipse.transcribe.diarize import assign_speakers
from eclipse.vault.writer import (
    _render_transcript_body,
    render_markdown,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seg(start: float, end: float, text: str = "") -> Segment:
    return Segment(start=start, end=end, text=text, words=[], speaker=None)


# ---------------------------------------------------------------------------
# assign_speakers — pure logic tests
# ---------------------------------------------------------------------------


def test_segment_fully_inside_turn_gets_that_speaker() -> None:
    segs = [_seg(1.0, 3.0)]
    turns = [(0.0, 5.0, "SPEAKER_00")]
    assign_speakers(segs, turns)
    assert segs[0].speaker == "SPEAKER_00"


def test_segment_straddling_two_turns_gets_max_overlap_speaker() -> None:
    # Segment [2, 6] — 2 s overlap with SPEAKER_00 [0,4], 2 s overlap with SPEAKER_01 [4,8].
    # Tie: the first one found keeps its lead (both equal); let's use an asymmetric case.
    segs = [_seg(2.0, 7.0)]
    turns = [
        (0.0, 4.0, "SPEAKER_00"),  # overlap = min(7,4) - max(2,0) = 4-2 = 2.0
        (4.0, 10.0, "SPEAKER_01"),  # overlap = min(7,10) - max(2,4) = 7-4 = 3.0
    ]
    assign_speakers(segs, turns)
    assert segs[0].speaker == "SPEAKER_01"


def test_segment_straddling_gets_first_on_exact_tie() -> None:
    # Both turns overlap the segment equally.
    segs = [_seg(2.0, 6.0)]
    turns = [
        (0.0, 4.0, "SPEAKER_00"),  # overlap = 4-2 = 2.0
        (4.0, 8.0, "SPEAKER_01"),  # overlap = 6-4 = 2.0
    ]
    assign_speakers(segs, turns)
    # Tie: SPEAKER_00 was found first and best_overlap stays at 2.0 (> not >=), so SPEAKER_00 wins.
    assert segs[0].speaker == "SPEAKER_00"


def test_segment_overlapping_nothing_stays_none() -> None:
    segs = [_seg(10.0, 12.0)]
    turns = [(0.0, 5.0, "SPEAKER_00"), (5.0, 9.9, "SPEAKER_01")]
    assign_speakers(segs, turns)
    assert segs[0].speaker is None


def test_empty_turns_leaves_all_none() -> None:
    segs = [_seg(0.0, 1.0), _seg(1.0, 2.0), _seg(2.0, 3.0)]
    assign_speakers(segs, [])
    assert all(s.speaker is None for s in segs)


def test_empty_segments_is_noop() -> None:
    turns = [(0.0, 5.0, "SPEAKER_00")]
    assign_speakers([], turns)  # must not raise


def test_multiple_segments_each_get_correct_speaker() -> None:
    segs = [_seg(0.0, 2.0), _seg(2.5, 4.5), _seg(5.0, 7.0)]
    turns = [
        (0.0, 3.0, "SPEAKER_00"),
        (3.0, 8.0, "SPEAKER_01"),
    ]
    assign_speakers(segs, turns)
    assert segs[0].speaker == "SPEAKER_00"  # [0,2] fully in [0,3]
    assert segs[1].speaker == "SPEAKER_01"  # [2.5,4.5]: 0.5 s in SPEAKER_00, 1.5 s in SPEAKER_01
    assert segs[2].speaker == "SPEAKER_01"  # [5,7] fully in [3,8]


def test_touching_boundary_is_not_overlap() -> None:
    # Segment starts exactly where turn ends — zero-width overlap should not assign.
    segs = [_seg(5.0, 7.0)]
    turns = [(0.0, 5.0, "SPEAKER_00")]  # overlap = min(7,5) - max(5,0) = 5-5 = 0.0
    assign_speakers(segs, turns)
    assert segs[0].speaker is None


def test_speaker_none_preserved_when_already_set_to_none() -> None:
    # Confirm existing None survives empty-turn call.
    seg = _seg(0.0, 1.0)
    seg.speaker = None
    assign_speakers([seg], [])
    assert seg.speaker is None


# ---------------------------------------------------------------------------
# TranscriptResult integration — ensure assign_speakers mutates the nested model
# ---------------------------------------------------------------------------


def test_assign_speakers_mutates_transcript_result_segments() -> None:
    tr = TranscriptResult(
        text="hello world",
        segments=[
            Segment(
                start=0.0, end=1.0, text="hello", words=[Word(start=0.0, end=0.5, word="hello")]
            ),
            Segment(start=1.5, end=3.0, text="world", words=[]),
        ],
    )
    turns = [(0.0, 2.0, "SPEAKER_00"), (2.0, 5.0, "SPEAKER_01")]
    assign_speakers(tr.segments, turns)
    assert tr.segments[0].speaker == "SPEAKER_00"  # [0,1] fully in [0,2]
    assert tr.segments[1].speaker == "SPEAKER_01"  # [1.5,3]: 0.5 s in [0,2], 1.0 s in [2,5]


# ---------------------------------------------------------------------------
# Writer rendering — speaker-labelled transcript
# ---------------------------------------------------------------------------


def test_render_transcript_body_with_speakers_produces_labelled_lines() -> None:
    segs = [
        Segment(start=0.0, end=2.0, text="Hello there.", speaker="SPEAKER_00"),
        Segment(start=2.0, end=4.0, text="How are you?", speaker="SPEAKER_01"),
        Segment(start=4.0, end=6.0, text="I am fine.", speaker="SPEAKER_00"),
    ]
    body = _render_transcript_body(segs, "fallback")
    assert "**SPEAKER_00:** Hello there." in body
    assert "**SPEAKER_01:** How are you?" in body
    assert "**SPEAKER_00:** I am fine." in body
    assert "fallback" not in body


def test_render_transcript_body_groups_consecutive_same_speaker() -> None:
    segs = [
        Segment(start=0.0, end=1.0, text="One.", speaker="SPEAKER_00"),
        Segment(start=1.0, end=2.0, text="Two.", speaker="SPEAKER_00"),
        Segment(start=2.0, end=3.0, text="Three.", speaker="SPEAKER_01"),
    ]
    body = _render_transcript_body(segs, "fallback")
    # SPEAKER_00's two consecutive segments must be merged into one block.
    assert body.count("**SPEAKER_00:**") == 1
    assert "One. Two." in body
    assert "**SPEAKER_01:** Three." in body


def test_render_transcript_body_no_speakers_returns_fallback_unchanged() -> None:
    segs = [
        Segment(start=0.0, end=1.0, text="Hello.", speaker=None),
        Segment(start=1.0, end=2.0, text="World.", speaker=None),
    ]
    fallback = "The full transcript text."
    body = _render_transcript_body(segs, fallback)
    assert body == fallback


def test_render_transcript_body_empty_segments_returns_fallback() -> None:
    body = _render_transcript_body([], "original text")
    assert body == "original text"


def test_render_markdown_with_speakers_renders_labels() -> None:
    """Full render_markdown path with diarized segments."""
    pm = ProcessedMeeting(
        source_name="rec.m4a",
        file_hash="deadbeef",
        meeting_date=date(2026, 6, 17),
        transcript=TranscriptResult(
            text="Hello there. How are you?",
            duration_sec=120,
            segments=[
                Segment(start=0.0, end=2.0, text="Hello there.", speaker="SPEAKER_00"),
                Segment(start=2.0, end=4.0, text="How are you?", speaker="SPEAKER_01"),
            ],
        ),
        insights=MeetingInsights(title="Test Meeting", summary="A test."),
    )
    md = render_markdown(pm)
    assert "**SPEAKER_00:** Hello there." in md
    assert "**SPEAKER_01:** How are you?" in md
    # Flat fallback text must NOT appear verbatim in diarized output.
    assert "Hello there. How are you?" not in md


def test_render_markdown_without_speakers_unchanged() -> None:
    """Existing non-diarized output must be byte-identical to pre-Phase-4 behaviour."""
    pm = ProcessedMeeting(
        source_name="rec.m4a",
        file_hash="abc123",
        meeting_date=date(2026, 6, 17),
        transcript=TranscriptResult(text="The full transcript text.", duration_sec=600),
        insights=MeetingInsights(title="Plain Meeting", summary="No speakers."),
    )
    md = render_markdown(pm)
    assert "The full transcript text." in md
    assert "**SPEAKER" not in md

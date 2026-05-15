"""Smoke test for --single-speaker path: parse VTT into timestamped lines."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

# Allow importing transcribe.py from the parent directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transcribe import parse_vtt_to_cues

FIXTURE = Path(__file__).resolve().parent / "fixture_100s.vtt"

# fmt: off
KNOWN_CUES = [
    (   3.67,    3.68, "I mean, there's no better presentation"),
    (   3.68,    6.71, "I mean, there's no better presentation software on the planet. It's really,"),
    (   6.71,    6.72, "software on the planet. It's really,"),
    (   6.72,    8.55, "software on the planet. It's really, you know, variable width fonts in"),
    (   8.55,    8.56, "you know, variable width fonts in"),
    (   8.56,   10.07, "you know, variable width fonts in terminals is what we've all been waiting"),
    (  10.07,   10.08, "terminals is what we've all been waiting"),
    (  10.08,   13.35, "terminals is what we've all been waiting for. Um, okay. So, you you know how like"),
    (  13.35,   13.36, "for. Um, okay. So, you you know how like"),
    (  13.36,   14.87, "for. Um, okay. So, you you know how like there's like an old somewhat over the"),
]
# fmt: on


class SingleSpeakerSmokeTest(unittest.TestCase):
    def test_parse_vtt_to_cues_yields_all_cues(self) -> None:
        """--single-speaker parses a YouTube VTT into the expected number of timed cues."""
        cues = list(parse_vtt_to_cues(FIXTURE))
        # YouTube auto-captions produce ~1 cue per 1-3s of audio;
        # 100s of speech should yield somewhere around 80-120 cues.
        self.assertGreaterEqual(len(cues), 60, "should have at least 60 cues in 100s")
        self.assertLessEqual(len(cues), 150, "should have at most 150 cues in 100s")

    def test_cue_timestamps_ascending(self) -> None:
        """All cues should have start <= end and be in chronological order."""
        cues = list(parse_vtt_to_cues(FIXTURE))
        prev_end = 0.0
        for start, end, text in cues:
            self.assertGreaterEqual(
                start, prev_end - 0.01,
                f"cue at {start:.2f}s should not start before previous end {prev_end:.2f}s",
            )
            self.assertLessEqual(
                start, end,
                f"cue start {start:.2f}s should be <= end {end:.2f}s",
            )
            prev_end = end

    def test_no_inline_vtt_tags_remain(self) -> None:
        """Inline VTT tags like <c>, <b> should be stripped."""
        cues = list(parse_vtt_to_cues(FIXTURE))
        for _, _, text in cues:
            self.assertNotIn("<c>", text, "<c> should be stripped")
            self.assertNotIn("</c>", text, "</c> should be stripped")
            self.assertNotIn("<b>", text, "<b> should be stripped")
            self.assertFalse(
                re.search(r"<[^>]+>", text),
                f"unexpected VTT tag in: {text[:80]}",
            )

    def test_no_blank_or_empty_utterances(self) -> None:
        """Every cue should have non-empty text."""
        cues = list(parse_vtt_to_cues(FIXTURE))
        for start, end, text in cues:
            with self.subTest(cue=f"{start:.2f}s"):
                self.assertGreater(len(text.strip()), 0, f"empty text at {start:.2f}s")

    def test_format_matches_diarization_style(self) -> None:
        """Format a few lines and verify they match the expected pattern."""
        cues = list(parse_vtt_to_cues(FIXTURE))
        lines = [f"[{start:8.2f} - {end:8.2f}] {text}" for start, end, text in cues]

        # Check first few lines match pattern
        self.assertRegex(
            lines[0],
            r"^\[\s*\d+\.\d{2}\s*-\s*\d+\.\d{2}\]\s+.+",
            "line should match [  XX.XX -   YY.YY] text",
        )

        # Verify the exact format from our first known cues
        for i, (expected_s, expected_e, expected_text) in enumerate(KNOWN_CUES):
            with self.subTest(cue_index=i):
                self.assertAlmostEqual(cues[i][0], expected_s, delta=0.01)
                self.assertAlmostEqual(cues[i][1], expected_e, delta=0.01)
                self.assertEqual(cues[i][2], expected_text)

    def test_start_time_clip_works(self) -> None:
        """When start_time is passed, earlier cues are skipped."""
        cues = list(parse_vtt_to_cues(FIXTURE, start_time=50.0))
        self.assertGreaterEqual(len(cues), 1)
        first_start = cues[0][0]
        self.assertGreaterEqual(first_start, 50.0 - 0.01)

    def test_duration_clip_works(self) -> None:
        """When duration is passed, only cues within the window are returned."""
        cues = list(parse_vtt_to_cues(FIXTURE, start_time=30.0, duration=10.0))
        self.assertGreaterEqual(len(cues), 1)
        for start, end, text in cues:
            self.assertGreaterEqual(start, 30.0 - 0.01)
            self.assertLess(start, 40.0, f"cue at {start:.2f}s should be < 40s")


if __name__ == "__main__":
    unittest.main()

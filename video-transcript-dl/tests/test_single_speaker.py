"""Smoke test for --single-speaker path: parse VTT into timestamped lines."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

# Allow importing transcribe.py from the parent directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from transcribe import _find_word_overlap, clean_vtt_cues, parse_vtt_to_cues

FIXTURE = Path(__file__).resolve().parent / "fixture_100s.vtt"

# Raw cues from YouTube auto-captions (before merge)
# fmt: off
RAW_CUES = [
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

# Merged cues (after clean_vtt_cues — no repeated half-sentences)
MERGED_CUES = [
    (   3.68,    6.71, "I mean, there's no better presentation software on the planet. It's really,"),
    (   6.72,    8.55, "you know, variable width fonts in"),
    (   8.56,   10.07, "terminals is what we've all been waiting"),
    (  10.08,   13.35, "for. Um, okay. So, you you know how like"),
    (  13.36,   14.87, "there's like an old somewhat over the"),
    (  14.88,   17.51, "hill person who like the world changes"),
    (  17.52,   19.43, "in some dramatic way and they use it as"),
    (  19.44,   21.75, "an excuse to say, you know what, now"),
    (  21.76,   24.15, "more than ever, all that stuff I always"),
    (  24.16,   26.31, "believed and told you, it's now now is"),
]
# fmt: on


class RawCueParsingTest(unittest.TestCase):
    """Tests for parse_vtt_to_cues — the raw VTT parser."""

    def test_yields_all_cues(self) -> None:
        cues = list(parse_vtt_to_cues(FIXTURE))
        self.assertGreaterEqual(len(cues), 60)
        self.assertLessEqual(len(cues), 150)

    def test_timestamps_ascending(self) -> None:
        cues = list(parse_vtt_to_cues(FIXTURE))
        prev_end = 0.0
        for start, end, _text in cues:
            self.assertGreaterEqual(
                start,
                prev_end - 0.01,
                f"cue at {start:.2f}s should not start before previous end {prev_end:.2f}s",
            )
            self.assertLessEqual(
                start, end, f"cue start {start:.2f}s should be <= end {end:.2f}s"
            )
            prev_end = end

    def test_no_inline_vtt_tags_remain(self) -> None:
        cues = list(parse_vtt_to_cues(FIXTURE))
        for _, _, text in cues:
            self.assertNotIn("<c>", text)
            self.assertNotIn("</c>", text)
            self.assertNotIn("<b>", text)
            self.assertFalse(re.search(r"<[^>]+>", text))

    def test_no_blank_or_empty_utterances(self) -> None:
        cues = list(parse_vtt_to_cues(FIXTURE))
        for start, _end, text in cues:
            with self.subTest(cue=f"{start:.2f}s"):
                self.assertGreater(len(text.strip()), 0)

    def test_format_matches_diarization_style(self) -> None:
        cues = list(parse_vtt_to_cues(FIXTURE))
        lines = [
            f"[{start:8.2f} - {end:8.2f}] {text}" for start, end, text in cues
        ]
        self.assertRegex(
            lines[0],
            r"^\[\s*\d+\.\d{2}\s*-\s*\d+\.\d{2}\]\s+.+",
        )

    def test_known_raw_cues(self) -> None:
        cues = list(parse_vtt_to_cues(FIXTURE))
        for i, (expected_s, expected_e, expected_text) in enumerate(RAW_CUES):
            with self.subTest(cue_index=i):
                self.assertAlmostEqual(cues[i][0], expected_s, delta=0.01)
                self.assertAlmostEqual(cues[i][1], expected_e, delta=0.01)
                self.assertEqual(cues[i][2], expected_text)

    def test_start_time_clip_works(self) -> None:
        cues = list(parse_vtt_to_cues(FIXTURE, start_time=50.0))
        self.assertGreaterEqual(len(cues), 1)
        self.assertGreaterEqual(cues[0][0], 50.0 - 0.01)

    def test_duration_clip_works(self) -> None:
        cues = list(parse_vtt_to_cues(FIXTURE, start_time=30.0, duration=10.0))
        self.assertGreaterEqual(len(cues), 1)
        for start, _end, _text in cues:
            self.assertGreaterEqual(start, 30.0 - 0.01)
            self.assertLess(start, 40.0)

    def test_clip_honors_start_boundary(self) -> None:
        """Cues before start_time should be excluded even when duration is unset."""
        cues = list(parse_vtt_to_cues(FIXTURE, start_time=10.0))
        for start, _end, _text in cues:
            self.assertGreaterEqual(start, 10.0 - 0.01)


class FindWordOverlapTest(unittest.TestCase):
    """Tests for _find_word_overlap."""

    def test_exact_match(self) -> None:
        self.assertEqual(
            _find_word_overlap("hello world", "hello world foo", min_words=1), 2
        )

    def test_partial_overlap(self) -> None:
        self.assertEqual(
            _find_word_overlap(
                "the quick brown fox",
                "brown fox jumps over",
                min_words=2,
            ),
            2,
        )

    def test_no_overlap(self) -> None:
        self.assertEqual(
            _find_word_overlap("hello world", "foo bar", min_words=2), 0
        )

    def test_single_word_below_min(self) -> None:
        self.assertEqual(
            _find_word_overlap("hello world", "world foo", min_words=2), 0
        )

    def test_below_min_words(self) -> None:
        self.assertEqual(
            _find_word_overlap("a b", "b c", min_words=3), 0
        )

    def test_full_overlap_shorter_second(self) -> None:
        self.assertEqual(
            _find_word_overlap("a b c d", "c d", min_words=1), 2
        )


class CleanVttCuesTest(unittest.TestCase):
    """Tests for clean_vtt_cues — merging overlapping YouTube cues."""

    def test_removes_short_display_cues(self) -> None:
        """Short overlay cues (~10ms) should be filtered out, roughly halving the count."""
        raw = list(parse_vtt_to_cues(FIXTURE))
        merged = list(clean_vtt_cues(FIXTURE))
        self.assertLess(len(merged), len(raw))
        # Should reduce by at least 30%
        self.assertLessEqual(len(merged), len(raw) * 0.7)

    def test_no_repeated_half_sentences(self) -> None:
        """No cue text should be a substring prefix of the following cue's text."""
        merged = list(clean_vtt_cues(FIXTURE))
        for i in range(len(merged) - 1):
            prev_text = merged[i][2]
            curr_text = merged[i + 1][2]
            with self.subTest(cue_index=i):
                # The current cue should not *start* with the end of the previous one
                overlap = _find_word_overlap(prev_text, curr_text, min_words=2)
                self.assertEqual(
                    overlap,
                    0,
                    f"still overlapping at line {i}: "
                    f"{prev_text[-40:]!r}  vs  {curr_text[:40]!r}",
                )

    def test_all_timestamps_valid(self) -> None:
        merged = list(clean_vtt_cues(FIXTURE))
        for start, end, _text in merged:
            self.assertLessEqual(start, end)
            self.assertGreaterEqual(start, 0.0)

    def test_known_merged_output(self) -> None:
        merged = list(clean_vtt_cues(FIXTURE))
        for i, (expected_s, expected_e, expected_text) in enumerate(MERGED_CUES):
            with self.subTest(cue_index=i):
                self.assertAlmostEqual(merged[i][0], expected_s, delta=0.01)
                self.assertAlmostEqual(merged[i][1], expected_e, delta=0.01)
                self.assertEqual(merged[i][2], expected_text)

    def test_start_time_clip(self) -> None:
        merged = list(clean_vtt_cues(FIXTURE, start_time=50.0))
        self.assertGreaterEqual(len(merged), 1)
        self.assertGreaterEqual(merged[0][0], 50.0 - 0.01)

    def test_duration_clip(self) -> None:
        merged = list(clean_vtt_cues(FIXTURE, start_time=30.0, duration=10.0))
        self.assertGreaterEqual(len(merged), 1)
        for start, _end, _text in merged:
            self.assertGreaterEqual(start, 30.0 - 0.01)
            self.assertLess(start, 40.0)

    def test_empty_vtt(self) -> None:
        """A VTT with no cues should yield nothing."""
        from pathlib import Path
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".vtt", delete=False
        ) as f:
            f.write("WEBVTT\n\n")
            empty_path = Path(f.name)

        try:
            merged = list(clean_vtt_cues(empty_path))
            self.assertEqual(merged, [])
        finally:
            empty_path.unlink()

    def test_no_long_cues(self) -> None:
        """A VTT with only short cues should yield nothing."""
        from pathlib import Path
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".vtt", delete=False
        ) as f:
            f.write("WEBVTT\n\n")
            f.write("00:00:01.000 --> 00:00:01.010\n")
            f.write("hello\n\n")
            f.write("00:00:01.020 --> 00:00:01.030\n")
            f.write("world\n")
            short_path = Path(f.name)

        try:
            merged = list(clean_vtt_cues(short_path))
            self.assertEqual(merged, [])
        finally:
            short_path.unlink()


if __name__ == "__main__":
    unittest.main()

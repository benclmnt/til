from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from spotify_downloader import SpotifyDownloader


class SpotifyDownloaderE2ETest(unittest.TestCase):
    EPISODE_URL = "https://open.spotify.com/episode/61MGd8uKcCFIcI8NOBArQM"

    @unittest.skipUnless(
        os.environ.get("RUN_E2E_SPOTIFY") == "1",
        "set RUN_E2E_SPOTIFY=1 to run networked Spotify E2E test",
    )
    def test_downloads_public_episode_clip(self) -> None:
        downloader = SpotifyDownloader()

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "clip.m4a"
            result_path = downloader.download_episode_audio(
                self.EPISODE_URL,
                output_path,
                duration=10,
            )

            self.assertEqual(result_path, output_path)
            self.assertTrue(output_path.exists())
            self.assertGreater(output_path.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()

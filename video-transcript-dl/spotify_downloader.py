from __future__ import annotations

import ast
import base64
from dataclasses import dataclass
import difflib
from email.utils import parsedate_to_datetime
import gzip
import hashlib
import hmac
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SpotifyEpisodePageInfo:
    episode_id: str
    file_ids: tuple[str, ...]
    episode_name: str
    show_name: str
    publisher_name: str
    duration_ms: int | None
    release_date_iso: str | None
    client_version: str
    correlation_id: str
    js_url: str


class SpotifyDownloader:
    """Resolve and download audio for public Spotify podcast episode URLs."""

    MOBILE_USER_AGENT = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    )
    WEB_USER_AGENT = "Mozilla/5.0"
    PRODUCT_TYPE = "web-player"
    CLIENT_ID_FALLBACK = "f6a40776580943a7bc5173125a1e8832"
    ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
    ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
    EPISODE_URL_RE = re.compile(
        r"(?:https?://)?open\.spotify\.com/episode/([A-Za-z0-9]+)",
        re.IGNORECASE,
    )
    FILE_ID_RE = re.compile(r"/mp3-preview/([0-9a-f]{40})", re.IGNORECASE)

    def __init__(self, *, ffmpeg_bin: str = "ffmpeg") -> None:
        self.ffmpeg_bin = ffmpeg_bin

    @classmethod
    def can_handle_url(cls, url: str) -> bool:
        return cls.episode_id_from_url(url) is not None

    @classmethod
    def episode_id_from_url(cls, url: str) -> str | None:
        match = cls.EPISODE_URL_RE.search(url)
        return match.group(1) if match else None

    def download_episode_audio(
        self,
        url: str,
        target_path: Path,
        *,
        start_time: float = 0.0,
        duration: float | None = None,
    ) -> Path:
        page_info = self._get_mobile_page_info(url)

        try:
            public_audio_url = self._resolve_public_episode_audio_url(page_info)
        except Exception as exc:
            raise RuntimeError(
                "Failed to resolve a public podcast audio URL for this Spotify episode. "
                "Spotify's web-player stream is DRM-protected, so Spotify-exclusive episodes "
                f"or feeds we can't match are not currently supported: {exc}"
            ) from exc

        return self._download_enclosure_audio(
            public_audio_url,
            target_path,
            start_time=start_time,
            duration=duration,
        )

    def resolve_episode_audio_url(self, url: str) -> str:
        page_info = self._get_mobile_page_info(url)
        try:
            return self._resolve_public_episode_audio_url(page_info)
        except Exception as exc:
            raise RuntimeError(
                "Failed to resolve a public podcast audio URL for this Spotify episode. "
                "Spotify's web-player stream is DRM-protected, so Spotify-exclusive episodes "
                f"or feeds we can't match are not currently supported: {exc}"
            ) from exc

    def _prepare_episode_download(self, url: str) -> tuple[SpotifyEpisodePageInfo, str, str]:
        page_info = self._get_mobile_page_info(url)
        js_text = self._fetch_text(
            page_info.js_url,
            headers={"User-Agent": self.MOBILE_USER_AGENT},
        )
        access_token = self._get_access_token(js_text)
        client_token = self._get_client_token(
            js_text,
            client_version=page_info.client_version,
            correlation_id=page_info.correlation_id,
        )
        return page_info, access_token, client_token

    def _read_url_bytes(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        data: bytes | None = None,
    ) -> bytes:
        request = urllib.request.Request(url, headers=headers or {}, data=data)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()

    def _download_file(
        self,
        url: str,
        target_path: Path,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> Path:
        request = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(request, timeout=timeout) as response, target_path.open("wb") as out_file:
            shutil.copyfileobj(response, out_file)
        return target_path

    @staticmethod
    def _decode_web_text(data: bytes) -> str:
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        return data.decode("utf-8", "replace")

    def _fetch_text(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        data: bytes | None = None,
    ) -> str:
        response_bytes = self._read_url_bytes(url, headers=headers, timeout=timeout, data=data)
        return self._decode_web_text(response_bytes)

    def _iter_embedded_json_objects(self, html: str):
        for body in re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL):
            text = body.strip()
            if not text or not re.fullmatch(r"[A-Za-z0-9+/=\s]+", text):
                continue
            try:
                decoded = base64.b64decode(text).decode("utf-8")
                yield json.loads(decoded)
            except Exception:
                continue

    def _get_mobile_page_info(self, url: str) -> SpotifyEpisodePageInfo:
        episode_id = self.episode_id_from_url(url)
        if episode_id is None:
            raise RuntimeError(f"Not a Spotify episode URL: {url}")

        html = self._fetch_text(
            url,
            headers={
                "User-Agent": self.MOBILE_USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

        client_version: str | None = None
        correlation_id: str | None = None
        file_ids: list[str] = []
        episode_key = f"spotify:episode:{episode_id}"
        episode_obj: dict | None = None

        for obj in self._iter_embedded_json_objects(html):
            if isinstance(obj, dict) and client_version is None and "clientVersion" in obj:
                raw_client_version = obj.get("clientVersion")
                raw_correlation_id = obj.get("correlationId")
                if isinstance(raw_client_version, str) and raw_client_version:
                    client_version = raw_client_version
                if isinstance(raw_correlation_id, str) and raw_correlation_id:
                    correlation_id = raw_correlation_id

            if not isinstance(obj, dict):
                continue

            episode = ((obj.get("entities") or {}).get("items") or {}).get(episode_key)
            if not isinstance(episode, dict):
                continue

            if episode_obj is None:
                episode_obj = episode

            for item in ((episode.get("audio") or {}).get("items") or []):
                raw_url = item.get("url") if isinstance(item, dict) else None
                if not isinstance(raw_url, str):
                    continue
                match = self.FILE_ID_RE.search(raw_url)
                if match:
                    file_ids.append(match.group(1))

        js_urls = re.findall(
            r'<script[^>]+src="([^"]+mobile-web-player[^"]+\.js)"',
            html,
            re.IGNORECASE,
        )

        if episode_obj is None:
            raise RuntimeError("Could not find Spotify episode metadata on the page")

        show_data = ((episode_obj.get("showOrAudiobook") or {}).get("data") or {})
        publisher = ((show_data.get("publisher") or {}).get("name"))
        duration_ms = ((episode_obj.get("duration") or {}).get("totalMilliseconds"))
        release_date_iso = ((episode_obj.get("releaseDate") or {}).get("isoString"))

        return SpotifyEpisodePageInfo(
            episode_id=episode_id,
            file_ids=tuple(dict.fromkeys(file_ids)),
            episode_name=str(episode_obj.get("name") or ""),
            show_name=str(show_data.get("name") or ""),
            publisher_name=str(publisher or ""),
            duration_ms=int(duration_ms) if isinstance(duration_ms, int) else None,
            release_date_iso=str(release_date_iso) if isinstance(release_date_iso, str) else None,
            client_version=client_version or "unknown",
            correlation_id=correlation_id or str(uuid.uuid4()),
            js_url=urllib.parse.urljoin(url, js_urls[0]) if js_urls else "",
        )

    @staticmethod
    def _normalize_match_text(text: str) -> str:
        return re.sub(r"[\W_]+", "", text.casefold())

    @classmethod
    def _string_similarity(cls, left: str, right: str) -> float:
        left_norm = cls._normalize_match_text(left)
        right_norm = cls._normalize_match_text(right)
        if not left_norm or not right_norm:
            return 0.0
        if left_norm == right_norm:
            return 1.0
        return difflib.SequenceMatcher(None, left_norm, right_norm).ratio()

    def _search_itunes_podcast_feeds(self, show_name: str, publisher_name: str) -> list[dict]:
        queries = [f"{show_name} {publisher_name}".strip(), show_name.strip()]
        results_by_feed: dict[str, dict] = {}

        for query in queries:
            if not query:
                continue

            params = urllib.parse.urlencode(
                {
                    "media": "podcast",
                    "entity": "podcast",
                    "limit": "10",
                    "term": query,
                }
            )
            payload = json.loads(
                self._fetch_text(
                    f"{self.ITUNES_SEARCH_URL}?{params}",
                    headers={"User-Agent": self.WEB_USER_AGENT, "Accept": "application/json"},
                    timeout=20.0,
                )
            )

            for result in payload.get("results") or []:
                if not isinstance(result, dict):
                    continue
                feed_url = result.get("feedUrl")
                if isinstance(feed_url, str) and feed_url:
                    results_by_feed.setdefault(feed_url, result)

        return list(results_by_feed.values())

    def _score_itunes_result(self, result: dict, *, show_name: str, publisher_name: str) -> float:
        collection_name = str(result.get("collectionName") or "")
        artist_name = str(result.get("artistName") or "")

        score = 100.0 * self._string_similarity(show_name, collection_name)
        if publisher_name:
            score += 35.0 * self._string_similarity(publisher_name, artist_name)

        if self._normalize_match_text(show_name) == self._normalize_match_text(collection_name):
            score += 25.0
        if publisher_name and self._normalize_match_text(publisher_name) == self._normalize_match_text(artist_name):
            score += 10.0

        return score

    @staticmethod
    def _parse_rss_duration_seconds(value: str | None) -> float | None:
        if not value:
            return None

        text = value.strip()
        if not text:
            return None

        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            return float(text)

        parts = text.split(":")
        try:
            total = 0.0
            for part in parts:
                total = total * 60 + float(part)
            return total
        except ValueError:
            return None

    @staticmethod
    def _parse_iso_date(value: str | None):
        if not value:
            return None
        try:
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            return parsedate_to_datetime(value)  # type: ignore[arg-type]
        except Exception:
            try:
                from datetime import datetime

                return datetime.fromisoformat(value)
            except Exception:
                return None

    @staticmethod
    def _parse_pub_date(value: str | None):
        if not value:
            return None
        try:
            return parsedate_to_datetime(value)
        except Exception:
            return None

    def _score_episode_item(self, item: ET.Element, page_info: SpotifyEpisodePageInfo) -> float:
        title = item.findtext("title") or ""
        enclosure = item.find("enclosure")
        if enclosure is None or not enclosure.attrib.get("url"):
            return float("-inf")

        score = 120.0 * self._string_similarity(page_info.episode_name, title)
        if self._normalize_match_text(page_info.episode_name) == self._normalize_match_text(title):
            score += 40.0

        expected_seconds = None if page_info.duration_ms is None else page_info.duration_ms / 1000.0
        actual_seconds = self._parse_rss_duration_seconds(item.findtext(f"{self.ITUNES_NS}duration"))
        if expected_seconds is not None and actual_seconds is not None:
            difference = abs(expected_seconds - actual_seconds)
            if difference <= 5:
                score += 30.0
            elif difference <= 60:
                score += 20.0
            elif difference <= 300:
                score += 8.0

        expected_date = self._parse_iso_date(page_info.release_date_iso)
        actual_date = self._parse_pub_date(item.findtext("pubDate"))
        if expected_date is not None and actual_date is not None:
            if expected_date.date() == actual_date.date():
                score += 20.0

        return score

    def _resolve_public_episode_audio_url(self, page_info: SpotifyEpisodePageInfo) -> str:
        if not page_info.show_name:
            raise RuntimeError("Spotify page did not include the show name")
        if not page_info.episode_name:
            raise RuntimeError("Spotify page did not include the episode title")

        search_results = self._search_itunes_podcast_feeds(
            page_info.show_name,
            page_info.publisher_name,
        )
        if not search_results:
            raise RuntimeError("could not find a matching public podcast feed")

        ranked_results = sorted(
            search_results,
            key=lambda result: self._score_itunes_result(
                result,
                show_name=page_info.show_name,
                publisher_name=page_info.publisher_name,
            ),
            reverse=True,
        )

        best_feed_url = ranked_results[0].get("feedUrl")
        best_score = self._score_itunes_result(
            ranked_results[0],
            show_name=page_info.show_name,
            publisher_name=page_info.publisher_name,
        )
        if not isinstance(best_feed_url, str) or not best_feed_url:
            raise RuntimeError("matched podcast feed entry did not contain a feed URL")
        if best_score < 60.0:
            raise RuntimeError(
                f"best public feed match looked too weak (score={best_score:.1f})"
            )

        feed_xml = self._read_url_bytes(
            best_feed_url,
            headers={"User-Agent": self.WEB_USER_AGENT, "Accept": "application/rss+xml, application/xml"},
            timeout=30.0,
        )
        root = ET.fromstring(feed_xml)
        channel = root.find("channel")
        if channel is None:
            raise RuntimeError("podcast feed did not contain a channel")

        items = channel.findall("item")
        if not items:
            raise RuntimeError("podcast feed did not contain any episodes")

        ranked_items = sorted(
            items,
            key=lambda item: self._score_episode_item(item, page_info),
            reverse=True,
        )
        best_item = ranked_items[0]
        best_item_score = self._score_episode_item(best_item, page_info)
        if best_item_score < 70.0:
            raise RuntimeError(
                f"could not confidently match the Spotify episode inside the public feed (score={best_item_score:.1f})"
            )

        enclosure = best_item.find("enclosure")
        if enclosure is None:
            raise RuntimeError("matched feed item did not include an enclosure")

        enclosure_url = enclosure.attrib.get("url")
        if not enclosure_url:
            raise RuntimeError("matched feed item enclosure did not include a URL")

        return enclosure_url

    @staticmethod
    def _guess_audio_suffix(audio_url: str) -> str:
        path = urllib.parse.unquote(urllib.parse.urlsplit(audio_url).path)
        suffix = Path(path).suffix.lower()
        return suffix if suffix else ".bin"

    def _download_enclosure_audio(
        self,
        audio_url: str,
        target_path: Path,
        *,
        start_time: float = 0.0,
        duration: float | None = None,
    ) -> Path:
        tmp_path = target_path.with_name(f"{target_path.stem}.partial{target_path.suffix}")
        source_suffix = self._guess_audio_suffix(audio_url)
        source_path = target_path.with_name(f"{target_path.stem}.source{source_suffix}")
        copy_safe_suffixes = {".m4a", ".mp4", ".aac", ".mov"}
        for path in (tmp_path, source_path):
            if path.exists():
                path.unlink()

        def run_ffmpeg(codec_args: list[str]) -> None:
            cmd = [self.ffmpeg_bin, "-y", "-loglevel", "error", "-nostdin"]
            if start_time > 0:
                cmd.extend(["-ss", f"{start_time:g}"])
            cmd.extend(["-i", str(source_path)])
            if duration is not None:
                cmd.extend(["-t", f"{duration:g}"])
            cmd.extend(["-vn", *codec_args, "-movflags", "+faststart", str(tmp_path)])
            subprocess.run(cmd, check=True)

        try:
            self._download_file(
                audio_url,
                source_path,
                headers={"User-Agent": self.WEB_USER_AGENT},
                timeout=60.0,
            )

            if source_path.suffix.lower() == target_path.suffix.lower() and start_time == 0 and duration is None:
                final_source = source_path
            else:
                if source_path.suffix.lower() in copy_safe_suffixes:
                    try:
                        run_ffmpeg(["-c:a", "copy"])
                    except subprocess.CalledProcessError:
                        if tmp_path.exists():
                            tmp_path.unlink()
                        run_ffmpeg(["-c:a", "aac", "-b:a", "192k"])
                else:
                    run_ffmpeg(["-c:a", "aac", "-b:a", "192k"])
                final_source = tmp_path

            if not final_source.exists() or final_source.stat().st_size == 0:
                raise RuntimeError("Spotify audio download finished, but no audio file was written")

            final_source.replace(target_path)
            return target_path
        finally:
            for path in (tmp_path, source_path):
                if path.exists():
                    path.unlink()

    @staticmethod
    def _extract_totp_secret(js_text: str) -> tuple[str, int]:
        marker = -1
        for needle in ("/api/server-time", "/api/token", "totpVer"):
            marker = js_text.find(needle)
            if marker != -1:
                break
        search_window = js_text[max(0, marker - 5000): marker + 2000] if marker != -1 else js_text

        matches: list[tuple[str, int]] = []
        for pattern, quote in (
            (r"secret:'((?:\\.|[^'])*)',version:(\d+)", "'"),
            (r'secret:"((?:\\.|[^"])*)",version:(\d+)', '"'),
        ):
            for raw_secret, raw_version in re.findall(pattern, search_window):
                secret = ast.literal_eval(f"{quote}{raw_secret}{quote}")
                matches.append((secret, int(raw_version)))

        if not matches:
            raise RuntimeError("Could not extract the Spotify web token secret")

        return max(matches, key=lambda item: item[1])

    @classmethod
    def _extract_client_id(cls, js_text: str) -> str:
        match = re.search(r'clientID:"([0-9a-f]{32})"', js_text, re.IGNORECASE)
        return match.group(1) if match else cls.CLIENT_ID_FALLBACK

    @staticmethod
    def _decode_totp_key(obfuscated_secret: str) -> bytes:
        values = [ord(char) ^ ((index % 33) + 9) for index, char in enumerate(obfuscated_secret)]
        return "".join(str(value) for value in values).encode("utf-8")

    @staticmethod
    def _generate_totp(secret_key: bytes, timestamp: float | None = None) -> str:
        counter = int((time.time() if timestamp is None else timestamp) // 30)
        digest = hmac.new(secret_key, counter.to_bytes(8, "big"), hashlib.sha1).digest()
        offset = digest[-1] & 0x0F
        code = (
            ((digest[offset] & 0x7F) << 24)
            | ((digest[offset + 1] & 0xFF) << 16)
            | ((digest[offset + 2] & 0xFF) << 8)
            | (digest[offset + 3] & 0xFF)
        ) % 1_000_000
        return f"{code:06d}"

    def _get_server_time(self) -> float | None:
        try:
            data = json.loads(
                self._fetch_text(
                    "https://open.spotify.com/api/server-time",
                    headers={"User-Agent": self.MOBILE_USER_AGENT},
                    timeout=10.0,
                )
            )
        except Exception:
            return None

        server_time = data.get("serverTime")
        if isinstance(server_time, (int, float)):
            return float(server_time)
        return None

    def _get_access_token(self, js_text: str) -> str:
        obfuscated_secret, version = self._extract_totp_secret(js_text)
        secret_key = self._decode_totp_key(obfuscated_secret)
        server_time = self._get_server_time()

        params = {
            "reason": "init",
            "productType": self.PRODUCT_TYPE,
            "totp": self._generate_totp(secret_key),
            "totpServer": self._generate_totp(secret_key, server_time) if server_time is not None else "unavailable",
            "totpVer": str(version),
        }

        response = json.loads(
            self._fetch_text(
                "https://open.spotify.com/api/token?" + urllib.parse.urlencode(params),
                headers={
                    "User-Agent": self.WEB_USER_AGENT,
                    "Accept": "application/json",
                },
            )
        )

        access_token = response.get("accessToken")
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("Could not get a Spotify access token")
        return access_token

    def _get_client_token(self, js_text: str, *, client_version: str, correlation_id: str) -> str:
        payload = {
            "client_data": {
                "client_version": client_version,
                "client_id": self._extract_client_id(js_text),
                "js_sdk_data": {
                    "device_brand": "unknown",
                    "device_model": "unknown",
                    "os": sys.platform,
                    "os_version": "unknown",
                    "device_id": correlation_id,
                    "device_type": "computer",
                },
            }
        }

        response = json.loads(
            self._fetch_text(
                "https://clienttoken.spotify.com/v1/clienttoken",
                headers={
                    "User-Agent": self.WEB_USER_AGENT,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                data=json.dumps(payload).encode("utf-8"),
            )
        )

        token = ((response.get("granted_token") or {}).get("token"))
        if not isinstance(token, str) or not token:
            raise RuntimeError("Could not get a Spotify client token")
        return token

    def _resolve_cdn_url(self, file_id: str, *, access_token: str, client_token: str) -> str:
        params = urllib.parse.urlencode(
            {
                "version": "10000000",
                "product": "9",
                "platform": "39",
                "alt": "json",
            }
        )
        response = json.loads(
            self._fetch_text(
                f"https://spclient.wg.spotify.com/storage-resolve/files/audio/interactive/{file_id}?{params}",
                headers={
                    "User-Agent": self.WEB_USER_AGENT,
                    "Accept": "application/json",
                    "Authorization": f"Bearer {access_token}",
                    "client-token": client_token,
                },
                timeout=20.0,
            )
        )

        cdn_urls = response.get("cdnurl") or []
        if not cdn_urls:
            raise RuntimeError(f"Spotify storage-resolve returned no CDN URLs for {file_id}")

        protection = response.get("protection")
        if protection == "cenc":
            raise RuntimeError(
                "Spotify storage-resolve returned a DRM-protected CENC stream, which ffmpeg cannot decode"
            )

        return str(cdn_urls[0])

    def _download_cdn_audio(
        self,
        cdn_url: str,
        target_path: Path,
        *,
        start_time: float = 0.0,
        duration: float | None = None,
    ) -> Path:
        tmp_path = target_path.with_name(f"{target_path.stem}.partial{target_path.suffix}")
        source_path = target_path.with_name(f"{target_path.stem}.source{target_path.suffix}")
        for path in (tmp_path, source_path):
            if path.exists():
                path.unlink()

        def run_ffmpeg(codec_args: list[str]) -> None:
            cmd = [self.ffmpeg_bin, "-y", "-loglevel", "error", "-nostdin"]
            if start_time > 0:
                cmd.extend(["-ss", f"{start_time:g}"])
            cmd.extend(["-i", str(source_path)])
            if duration is not None:
                cmd.extend(["-t", f"{duration:g}"])
            cmd.extend(["-vn", *codec_args, "-movflags", "+faststart", str(tmp_path)])
            subprocess.run(cmd, check=True)

        try:
            self._download_file(
                cdn_url,
                source_path,
                headers={"User-Agent": self.WEB_USER_AGENT},
                timeout=60.0,
            )

            if start_time == 0 and duration is None:
                final_source = source_path
            else:
                try:
                    run_ffmpeg(["-acodec", "copy"])
                except subprocess.CalledProcessError:
                    if tmp_path.exists():
                        tmp_path.unlink()
                    run_ffmpeg(["-c:a", "aac", "-b:a", "192k"])
                final_source = tmp_path

            if not final_source.exists() or final_source.stat().st_size == 0:
                raise RuntimeError("Spotify audio download finished, but no audio file was written")

            final_source.replace(target_path)
            return target_path
        finally:
            for path in (tmp_path, source_path):
                if path.exists():
                    path.unlink()

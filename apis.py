"""
apis.py — the non-PowerShell tools: weather, search, time, location.

- Weather: Open-Meteo (no API key required).
- Search: NOT an API. See WebSearchAPI's docstring -- builds a real
  search-engine URL and opens it in Chrome, no search API/scraping.
- Time/date: pure Python, zero network calls.
- Location: IP-based geolocation, fetched once and cached for the process
  lifetime so the user is never asked and it's never re-fetched every turn.
"""

import requests
import time
from datetime import datetime
from typing import Dict, Optional
from urllib.parse import quote_plus


# Every API-layer function below that can fail returns a short,
# human-readable string on failure, starting with one of these prefixes --
# callers (see orchestrator.py's api-kind dispatch) check this list to
# decide whether there's a real result to narrate or just a failure to
# report plainly, instead of asking the local narration model to make
# sense of a technical error string. Confirmed live: handing the model a
# raw exception dump ("HTTPSConnectionPool(...NameResolutionError...")
# and asking it to "narrate this in one sentence" produced a fluent but
# completely disconnected sentence about something else entirely, since
# there was nothing coherent in the error text for it to paraphrase.
FAILURE_PREFIXES = (
    "Couldn't find", "Couldn't determine", "Weather lookup failed",
    "Forecast lookup failed", "Search failed", "Nothing is selected",
    "Couldn't convert", "Couldn't resize", "Couldn't compress", "Couldn't extract",
    "Couldn't detect", "Couldn't download", "Couldn't organize", "Can't organize",
    "Couldn't group", "Can't do that",
)


def is_api_failure(result: str) -> bool:
    return any(result.startswith(p) for p in FAILURE_PREFIXES)


def _friendly_request_error(e: Exception, action: str, service: str = "the service") -> str:
    """Classifies a requests exception into a short, honest, non-technical
    reason. Never a raw exception repr -- that's both unpleasant to read
    and unusable as narration-model input (see FAILURE_PREFIXES above)."""
    if isinstance(e, requests.exceptions.ConnectionError):
        return f"{action} failed: can't reach the internet right now."
    if isinstance(e, requests.exceptions.Timeout):
        return f"{action} failed: the request took too long."
    if isinstance(e, requests.exceptions.HTTPError):
        return f"{action} failed: {service} returned an error."
    return f"{action} failed: something went wrong on this end."


class LocationCache:
    """Fetched once at startup (or on first use), then reused all session.

    BETA 0.3.28 fix: a transient failure against ipinfo.io (network
    hiccup, DNS blip, rate limit, etc.) used to set self._cached to the
    all-zero/failed fallback dict, and `if self._cached is not None:
    return` treated that identically to a genuine successful fetch --
    permanently degrading every location-dependent feature (weather with
    no city given, etc.) for the rest of the session, with no TTL and no
    retry. Fixed the same way as app_control.py's AppController app cache:
    only a REAL success is ever stored in self._cached; a failure is never
    cached and just returns the zero-fallback for THIS call, recording
    when it happened so a burst of calls during an outage doesn't all pay
    the network-timeout cost -- the next call after _FAILURE_RETRY_SECONDS
    retries for real.
    """

    _FAILURE_RETRY_SECONDS = 30.0  # real network call -- don't hammer
                                     # ipinfo.io on every single call
                                     # during an outage, but do recover
                                     # once it's back

    def __init__(self):
        self._cached: Optional[Dict] = None
        self._last_failure_time: Optional[float] = None

    def get(self) -> Dict:
        if self._cached is not None:
            return self._cached

        now = time.time()
        if (
            self._last_failure_time is not None
            and (now - self._last_failure_time) < self._FAILURE_RETRY_SECONDS
        ):
            return {"city": "", "region": "", "country": "", "lat": 0.0, "lon": 0.0}

        try:
            resp = requests.get("https://ipinfo.io/json", timeout=5)
            resp.raise_for_status()
            data = resp.json()
            loc = data.get("loc", "")  # "lat,lon"
            lat, lon = (loc.split(",") + ["0", "0"])[:2]
            self._cached = {
                "city": data.get("city", "Unknown"),
                "region": data.get("region", ""),
                "country": data.get("country", ""),
                "lat": float(lat),
                "lon": float(lon),
            }
            self._last_failure_time = None
        except Exception:
            # Fail soft — location features just won't have a default city
            # for THIS call; not cached, so the next call retries.
            self._last_failure_time = now
            return {"city": "", "region": "", "country": "", "lat": 0.0, "lon": 0.0}
        return self._cached


location_cache = LocationCache()


class WeatherAPI:
    """Open-Meteo — free, no API key, no rate-limit headaches."""

    GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

    def _geocode(self, city: str) -> Optional[Dict]:
        try:
            resp = requests.get(self.GEOCODE_URL, params={"name": city, "count": 1}, timeout=5)
            resp.raise_for_status()
            results = resp.json().get("results")
            if not results:
                return None
            r = results[0]
            return {"lat": r["latitude"], "lon": r["longitude"], "name": r["name"]}
        except Exception:
            return None

    def _coords_for(self, city: str) -> Optional[Dict]:
        if city:
            return self._geocode(city)
        loc = location_cache.get()
        if loc["lat"] or loc["lon"]:
            return {"lat": loc["lat"], "lon": loc["lon"], "name": loc["city"] or "your area"}
        return None

    def get_weather(self, city: str = "") -> str:
        coords = self._coords_for(city)
        if not coords:
            return f"Couldn't find weather for '{city}'." if city else "Couldn't determine your location for weather."
        try:
            resp = requests.get(self.FORECAST_URL, params={
                "latitude": coords["lat"], "longitude": coords["lon"],
                "current": "temperature_2m,weather_code,wind_speed_10m",
            }, timeout=5)
            resp.raise_for_status()
            cur = resp.json().get("current", {})
            temp = cur.get("temperature_2m")
            wind = cur.get("wind_speed_10m")
            return f"{coords['name']}: {temp}\u00b0C, wind {wind} km/h"
        except Exception as e:
            return _friendly_request_error(e, "Weather lookup", "the weather service")

    def get_forecast(self, city: str = "", days: str = "3") -> str:
        coords = self._coords_for(city)
        if not coords:
            return f"Couldn't find forecast for '{city}'." if city else "Couldn't determine your location for forecast."
        try:
            n = max(1, min(int(days or 3), 7))
        except ValueError:
            n = 3
        try:
            resp = requests.get(self.FORECAST_URL, params={
                "latitude": coords["lat"], "longitude": coords["lon"],
                "daily": "temperature_2m_max,temperature_2m_min",
                "forecast_days": n,
            }, timeout=5)
            resp.raise_for_status()
            daily = resp.json().get("daily", {})
            dates = daily.get("time", [])
            highs = daily.get("temperature_2m_max", [])
            lows  = daily.get("temperature_2m_min", [])
            lines = [f"{d}: {hi}\u00b0C / {lo}\u00b0C" for d, hi, lo in zip(dates, highs, lows)]
            return f"{coords['name']} forecast:\n" + "\n".join(lines)
        except Exception as e:
            return _friendly_request_error(e, "Forecast lookup", "the weather service")


class WebSearchAPI:
    """
    BETA 0.3.44 architecture pivot -- search is NOT an API call anymore.

    WHY THIS REPLACED THE WIKIPEDIA/DUCKDUCKGO IMPLEMENTATION: that version
    answered "what is X" from a knowledge-graph API, and only for topics
    with a Wikipedia-style abstract -- it was never a real web search, and
    for ordinary conversational queries it just returned nothing. It also
    baked in a permanent ceiling: TOKI's search was always going to be as
    good as Wikipedia's summary API, never as good as an actual search
    engine, no matter how much more work went into it.

    The decision (finalized, not a workaround): TOKI has no native web
    search capability at all, and isn't meant to. The pipeline is

        query -> build a real search-engine URL -> open it in Chrome

    Chrome IS the search capability -- "Toki's infinite web extension".
    No search API, no HTML scraping, no fake AI search layer sitting in
    between the user and real results. This also means search literally
    cannot go stale or get rate-limited: it's exactly as good as Google
    (or YouTube/GitHub for the specialized cases below) always is.

    SEARCH_URLS covers the specialized, native-search-URL cases named in
    the design doc (YouTube/GitHub/Maps use their own real search UIs, not
    a generic web search of them) -- wired up here so a future intent
    (SEARCH_YOUTUBE etc.) can pass site= without touching this class again.
    Deliberately NOT auto-detected from the query text in this checkpoint:
    guessing "youtube" out of arbitrary free text risks exactly the kind of
    invented structure this codebase's slot-extraction already avoids
    everywhere else (see orchestrator.py's chain-split docstring) -- e.g.
    "search for youtube alternatives" would wrongly get routed to YouTube's
    search box instead of Google's. site defaults to "web" until a real,
    unambiguous slot (a separate intent, not text-sniffing) supplies it.
    """

    SEARCH_URLS = {
        "web": "https://www.google.com/search?q={q}",
        "youtube": "https://www.youtube.com/results?search_query={q}",
        "github": "https://github.com/search?q={q}",
        "maps": "https://www.google.com/maps/search/{q}/",
    }

    # Same bug class app_control.py's _escape_ps_slot() docstring covers
    # (Assassin's Creed breaking Start-Process 'Assassin's Creed') -- needed
    # again here, separately, because this builds its own PowerShell command
    # string directly rather than going through orchestrator.py's
    # centralized "powershell"-kind escaping (this is an "api"-kind intent,
    # never routed through that path).
    @staticmethod
    def _escape_ps_slot(value: str) -> str:
        return value.replace("'", "''")

    def _open_in_chrome(self, url: str) -> None:
        """
        Launches Chrome specifically (not just "the default browser") with
        url as its argument, checking the three real install locations
        directly rather than trusting chrome.exe to be on PATH -- it isn't,
        by default, on a normal Chrome install. Fails open: if Chrome truly
        isn't installed, hands the URL to Start-Process bare, which opens
        whatever the user's actual default browser is, same fail-open
        posture as AppController.launch_app().
        """
        import subprocess

        safe_url = self._escape_ps_slot(url)
        ps_cmd = (
            "$p = @("
            "\"$env:ProgramFiles\\Google\\Chrome\\Application\\chrome.exe\", "
            "\"${env:ProgramFiles(x86)}\\Google\\Chrome\\Application\\chrome.exe\", "
            "\"$env:LocalAppData\\Google\\Chrome\\Application\\chrome.exe\""
            ") | Where-Object { Test-Path $_ } | Select-Object -First 1; "
            f"if ($p) {{ Start-Process -FilePath $p -ArgumentList '{safe_url}' }} "
            f"else {{ Start-Process '{safe_url}' }}"
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )

    def search(self, query: str, site: str = "web", num_results: int = 3) -> str:
        # num_results kept as an accepted-but-unused param: orchestrator.py's
        # ToolDispatcher.call() only drops a slot on TypeError, and nothing
        # about removing it here is worth risking that path for.
        if not query:
            return "No search query given."

        site_key = (site or "web").lower().strip()
        template = self.SEARCH_URLS.get(site_key, self.SEARCH_URLS["web"])
        url = template.format(q=quote_plus(query))

        # BETA 0.3.56: YouTube results open through a dedicated,
        # CDP-debug-enabled browser instance instead of the plain
        # _open_in_chrome() path below -- see
        # video_downloader/media_browser.py's module docstring for the
        # full reasoning. This is what makes DOWNLOAD_PLAYING_VIDEO's
        # CDP fast path (cdp_now_playing.py) actually fire for videos
        # TOKI itself opened, instead of silently never having a debug
        # port to talk to. Never touches the user's real/main browser;
        # any failure here (browser not found, launch error) falls
        # straight through to the exact same plain-Chrome path every
        # other search already uses.
        if site_key == "youtube":
            try:
                from video_downloader.media_browser import launch_media_browser
                if launch_media_browser(url):
                    return f"Searching YouTube for '{query}' in TOKI's media browser."
            except Exception:
                pass  # fall through to the plain path below

        try:
            self._open_in_chrome(url)
        except Exception as e:
            return f"Search failed: couldn't open Chrome ({e})."

        return f"Searching for '{query}' in Chrome."


class TimeAPI:
    def get_time(self) -> str:
        return datetime.now().strftime("%I:%M %p")

    def get_date(self) -> str:
        return datetime.now().strftime("%A, %B %d, %Y")


class LocationAPI:
    def get_location(self) -> str:
        loc = location_cache.get()
        if not loc["city"]:
            return "Couldn't determine your location."
        parts = [p for p in (loc["city"], loc["region"], loc["country"]) if p]
        return ", ".join(parts)

    def get_raw_location(self) -> dict:
        """
        The dict form (city/region/country/lat/lon), for callers that need
        more than the formatted sentence -- e.g. showing a short status on
        startup once the fetch completes.
        """
        return location_cache.get()


class FileConvertAPI:
    """
    Thin adapter between orchestrator.py's api-kind dispatch and
    conversion_engine/. Every method here takes plain string slots (the
    same shape extract_slots() produces for every other api-kind intent)
    and returns a short, human-readable sentence -- never a raw path,
    never a traceback -- matching this file's existing convention (see
    WeatherAPI/WebSearchAPI above and is_api_failure()/FAILURE_PREFIXES).

    Every method resolves "the selected file" itself rather than trusting
    a stale slot, by calling selection_context.get_selection_context()
    directly -- so "shrink this image" always acts on whatever is
    CURRENTLY selected, even if the user selected something new between
    turns without saying so explicitly again.
    """

    def _current_selection_path(self) -> Optional[str]:
        from selection_context import get_selection_context
        sel = get_selection_context().get_selected()
        return sel["path"] if sel else None

    def convert_selected(self, target_format: str = "", explicit_source: str = "") -> str:
        # BETA 0.3.66 (widget-context merge session): confirmed live, a
        # real usability gap -- this method had no way to accept an
        # explicit filename at all, so "convert notes.txt to markdown"
        # (completely unambiguous) always fell through to "nothing is
        # selected right now" unless the user had ALSO just dragged that
        # exact file onto TOKI. explicit_source (from extractor.py's
        # _extract_convert_source(), already resolved to a real sandboxed
        # path) is tried first; falls back to the existing drag-drop
        # selection_context lookup exactly as before when no filename was
        # typed. Deliberately does NOT touch orchestrator._last_touched --
        # see SELECTION_ELIGIBLE_INTENTS's docstring in extractor.py for
        # why that separation is intentional and stays as-is here.
        path = explicit_source or self._current_selection_path()
        if not path:
            return "Nothing is selected right now -- drag a file onto TOKI first, or name the file directly (e.g. \"convert notes.txt to markdown\")."
        if not target_format:
            return "Couldn't convert: I need to know what format to convert to."
        try:
            from conversion_engine import convert_file
            out = convert_file(path, target_ext=target_format)
            return f"Converted to {out}"
        except Exception as e:
            return f"Couldn't convert that file: {e}"

    def resize_selected(self, width: str = "", height: str = "", scale: str = "") -> str:
        path = self._current_selection_path()
        if not path:
            return "Nothing is selected right now -- drag a file onto TOKI first."
        try:
            from conversion_engine import resize_file
            kwargs = {}
            if width:
                kwargs["width"] = int(width)
            if height:
                kwargs["height"] = int(height)
            if scale:
                kwargs["scale"] = float(scale)
            out = resize_file(path, **kwargs)
            return f"Resized to {out}"
        except Exception as e:
            return f"Couldn't resize that file: {e}"

    def compress_selected(self, quality: str = "") -> str:
        path = self._current_selection_path()
        if not path:
            return "Nothing is selected right now -- drag a file onto TOKI first."
        try:
            from conversion_engine import compress_file
            kwargs = {"quality": int(quality)} if quality else {}
            out = compress_file(path, **kwargs)
            return f"Compressed to {out}"
        except Exception as e:
            return f"Couldn't compress that file: {e}"

    def extract_selected(self) -> str:
        path = self._current_selection_path()
        if not path:
            return "Nothing is selected right now -- drag a file onto TOKI first."
        try:
            from conversion_engine import extract_file
            out = extract_file(path)
            return f"Extracted to {out}"
        except Exception as e:
            return f"Couldn't extract that file: {e}"


class VideoDownloadAPI:
    """
    Thin adapter between orchestrator.py's api-kind dispatch and
    video_downloader/ -- same shape as FileConvertAPI just above: plain
    string slots in, one short human-readable sentence out, never a raw
    path or traceback.

    download_playing() resolves ITS OWN url at call time via
    video_downloader.now_playing.get_now_playing_url() -- which itself
    tries video_downloader/cdp_now_playing.py first (which open tab
    actually has a video playing right now, via Chrome DevTools
    Protocol) and falls back to the focused browser's address bar if
    CDP isn't reachable -- rather than trusting anything
    extract_slots() pulled from the user's phrasing -- there's nothing
    for the user to have said that WOULD contain that URL, unlike
    download_url(), which always comes from an explicit link the user
    typed or pasted.
    """

    def download_playing(self, audio_only: str = "") -> str:
        from video_downloader.now_playing import get_now_playing_url
        url = get_now_playing_url()
        if not url:
            return (
                "Couldn't detect what's playing -- make sure the video's "
                "browser tab is the focused window, or just paste me the link."
            )
        return self._download(url, audio_only)

    def download_url(self, url: str = "", audio_only: str = "") -> str:
        if not url:
            return "Couldn't find a link in that -- send me the video's URL."
        return self._download(url, audio_only)

    def _download(self, url: str, audio_only: str) -> str:
        try:
            from video_downloader import download_video, ffmpeg_available
            out = download_video(url, audio_only=bool(audio_only))
            # BETA 0.3.67: video downloads no longer HARD-require ffmpeg
            # (see FfmpegNotFoundError's docstring in video_downloader --
            # a plain "best" pre-muxed stream needs no merging), but a
            # merge-capable machine usually gets a higher resolution.
            # Flag that trade-off here rather than silently under-
            # delivering with no explanation.
            if not audio_only and not ffmpeg_available():
                return (
                    f"Downloaded to {out} (installed ffmpeg would let me "
                    "grab a higher resolution -- this one used the "
                    "single best pre-combined stream available instead)."
                )
            return f"Downloaded to {out}"
        except Exception as e:
            return f"Couldn't download that video: {e}"


class FileOrganizerAPI:
    """
    Thin adapter between orchestrator.py's api-kind dispatch and
    file_graph/ -- same shape as FileConvertAPI/VideoDownloadAPI above:
    plain string slots in, one human-readable sentence out, never a raw
    traceback. See file_graph/organizer.py's own (much longer) docstring
    for the actual scan/score/execute pipeline and its safety guarantees
    (never invents a destination folder, sandbox-checked twice, never
    overwrites an existing file).

    `path` always arrives already resolved by extractor.py (defaults to
    the real Desktop when the user didn't name a folder, same
    `_default_root_for()` fallback SORT_FOLDER_BY_TYPE already uses) --
    this class never has to guess a root itself.

    `include_suggestions` arrives as the string "true" or "" (extractor.py's
    usual string-slot convention -- see its own ORGANIZE_FILES_BY_TOPIC
    branch for the exact trigger phrases), not a real bool, so it's
    coerced here with a plain truthiness check.
    """

    def __init__(self):
        from file_graph.organizer import FileOrganizer
        self._organizer = FileOrganizer()

    def organize(self, path: str = "", include_suggestions: str = "") -> str:
        if not path:
            return "Couldn't find a folder to organize -- which one did you mean?"
        try:
            return self._organizer.organize(path, include_suggestions=bool(include_suggestions))
        except Exception as e:
            return f"Couldn't organize {path}: {e}"


class FileGroupingAPI:
    """
    Thin adapter for GROUP_FILES_BY_EXTENSION -- see file_grouping.py's
    own docstring for why this is a separate, simpler module from
    FileOrganizerAPI/file_graph/ above rather than a variant of it: no
    scoring, no confidence, a fully explicit instruction executed as-is.

    `extensions` arrives as a comma-joined string, not a real list --
    same string-only-slot convention every api-kind action in this file
    already follows (extract_slots() always returns Dict[str, str]), so
    it's split back apart here.
    """

    def __init__(self):
        from file_grouping import group_files_by_extension
        self._group = group_files_by_extension

    def group(self, path: str = "", extensions: str = "", dest_name: str = "") -> str:
        if not path or not extensions or not dest_name:
            return "Couldn't determine which file types or what to name the new folder -- can you spell that out?"
        ext_list = [e.strip() for e in extensions.split(",") if e.strip()]
        try:
            return self._group(path, ext_list, dest_name)
        except Exception as e:
            return f"Couldn't group those files: {e}"

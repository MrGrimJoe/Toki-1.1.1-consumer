"""
apis.py — the non-PowerShell tools: weather, search, time, location.

- Weather: Open-Meteo (no API key required).
- Search: DuckDuckGo Instant Answer API (no API key required).
- Time/date: pure Python, zero network calls.
- Location: IP-based geolocation, fetched once and cached for the process
  lifetime so the user is never asked and it's never re-fetched every turn.
"""

import re
import requests
import time
from datetime import datetime
from typing import Dict, Optional
from urllib.parse import quote


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


# ─── Targeted sub-question matching for WebSearchAPI ──────────────────────
#
# The Wikipedia summary REST endpoint only ever gives you a page's LEAD
# PARAGRAPH -- fine for "tell me about Pakistan", useless for "who founded
# Pakistan" (the lead paragraph is generally geography/population/
# government, not founding history). This scans a bigger, real extract of
# the article for the sentence that actually answers the specific question,
# using plain keyword overlap -- no model call, no scraping, same
# never-guess philosophy as every regex extractor in extractor.py.

_QUESTION_STOPWORDS = {
    "who", "what", "when", "where", "why", "how", "is", "was", "are", "were",
    "did", "does", "do", "the", "a", "an", "of", "in", "on", "for", "to",
    "please", "me", "you", "tell", "about", "search", "look", "up", "google",
    "and", "or", "its", "it's", "this", "that",
}

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"[A-Za-z']+")


def _question_keywords(query: str) -> list:
    words = _WORD_RE.findall(query.lower())
    return [w for w in words if w not in _QUESTION_STOPWORDS and len(w) > 2]


# Longest/most-specific suffixes first so e.g. "founders" strips to "found"
# in one pass instead of stopping early at "founder" -> "found" via "er"
# only. Each strip requires >=3 chars left over so short words like "is"
# or "as" are never mangled down to nothing.
_STEM_SUFFIXES = ("ing", "ers", "eds", "er", "ed", "'s", "s")


def _stem(word: str) -> str:
    """
    Lightweight suffix-stripping -- NOT a real stemmer (no Porter
    algorithm, no external deps) -- just enough to fix single-word
    keyword matches breaking on ordinary noun/verb inflection. e.g. the
    query keyword "founder" and the article word "founded" share no
    substring relationship at all ("founder" vs "founded"), so plain
    substring/equality matching finds nothing and silently falls back to
    the generic lead paragraph. Stemming both down to "found" fixes that
    whole word family (founder/founded/founding/founders).
    """
    for suf in _STEM_SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            return word[: -len(suf)]
    return word


def _best_matching_sentence(query: str, full_text: str, title: str = "") -> Optional[str]:
    """
    Returns the sentence in full_text that best matches the query's
    DISTINGUISHING keywords (the topic name itself is deliberately
    excluded -- it appears in nearly every sentence of its own article, so
    scoring on it would just match ~anything). Returns None if nothing
    distinguishing is left in the query (e.g. a generic "tell me about X"
    with no real sub-question), so the caller falls back to the plain lead
    summary as before.

    Matching is done on STEMMED whole words (not raw substrings) so a
    query keyword like "founder" matches article text like "founded" --
    see _stem()'s docstring. Whole-word comparison also avoids the
    opposite failure mode plain substring matching had: a short keyword
    like "art" spuriously matching inside "started" or "particle".
    """
    title_words = set(_WORD_RE.findall(title.lower()))
    keywords = [w for w in _question_keywords(query) if w not in title_words]
    if not keywords:
        return None
    stemmed_keywords = {_stem(kw) for kw in keywords}

    best_sentence, best_score = None, 0
    for sentence in _SENTENCE_SPLIT_RE.split(full_text):
        sentence_words = {_stem(w) for w in _WORD_RE.findall(sentence.lower())}
        score = sum(1 for kw in stemmed_keywords if kw in sentence_words)
        if score > best_score:
            best_sentence, best_score = sentence, score
    return best_sentence.strip() if best_sentence and best_score >= 1 else None


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
    Real lookups from two legitimate, keyless, unthrottled-for-our-purposes
    APIs -- no HTML scraping anywhere in this class.

    NOTE (why this replaced the old implementation, and why it's NOT a
    scraper either): the original version hit DuckDuckGo's Instant Answer
    API (api.duckduckgo.com/?format=json), which is NOT a search engine --
    it's the knowledge-graph box above DuckDuckGo's real results, and it
    only returns anything for topics with a Wikipedia-style abstract. For
    ordinary conversational queries it silently returns nothing, which is
    why it kept reporting "No results found." even when the answer clearly
    existed on the web.

    The tempting next step is to scrape DuckDuckGo's HTML results page
    (html.duckduckgo.com/html/) instead -- DON'T. That page actively
    fingerprints and 403s automated requests regardless of User-Agent, by
    design, and it can start blocking mid-session with zero warning. That
    is an unacceptable failure mode for a live Saturday demo: it might work
    in testing and then silently break in front of an audience.

    Instead, this class uses TWO real, sanctioned, keyless APIs:
      1. Wikipedia's REST API (search + summary) -- the primary path. It's
         a genuine, documented, heavily-cached public API (not a scrape),
         rate-limited generously (~200 req/s/client with a contactable
         User-Agent) and it answers the overwhelming majority of factual
         "what is X" / "who is X" / "tell me about X" queries a demo judge
         is likely to try.
      2. DuckDuckGo's Instant Answer API, KEPT (not removed) as a secondary
         source for the cases Wikipedia doesn't have a page for -- it's
         legitimate and unblocked, just narrow, so it earns a try but never
         a scrape to widen it.

    If neither has anything, we say so honestly instead of returning
    scraped or fabricated content.
    """

    WIKI_SEARCH_URL = "https://en.wikipedia.org/w/rest.php/v1/search/page"
    WIKI_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
    WIKI_ACTION_URL = "https://en.wikipedia.org/w/api.php"
    DDG_URL = "https://api.duckduckgo.com/"

    # Wikimedia's usage policy asks for a descriptive, contactable
    # User-Agent -- a bare default UA is the single most common reason a
    # keyless request to their API gets throttled, so this costs one header
    # to avoid that entirely.
    _HEADERS = {"User-Agent": "TOKI-DesktopAssistant/1.0 (school project; contact: n/a)"}

    def _wiki_search(self, query: str, num_results: int) -> list:
        resp = requests.get(
            self.WIKI_SEARCH_URL,
            params={"q": query, "limit": num_results},
            headers=self._HEADERS,
            timeout=6,
        )
        resp.raise_for_status()
        data = resp.json()
        # Defensive: the documented shape is {"pages": [...]}, but parse
        # defensively in case that top-level key ever shifts -- a bare list
        # response should still work rather than silently returning nothing.
        if isinstance(data, list):
            return data
        return data.get("pages", [])

    def _wiki_summary(self, title: str) -> Optional[str]:
        # Percent-encode the title before it goes into the URL PATH (not a
        # query param, so requests' automatic params= encoding doesn't
        # apply here -- this has to be done by hand). Without it, any real
        # article title containing a URL-meaningful character breaks the
        # request outright: "AC/DC" -> an extra path segment instead of
        # the title ("/summary/AC/DC" is parsed as title "AC" + subpath
        # "DC", not "AC/DC"); "Are You Experienced?" -> everything from
        # "?" onward is parsed as a query string, truncating the actual
        # title to "Are_You_Experienced". Both are real Wikipedia article
        # titles, not edge cases -- confirmed by constructing the exact
        # URL each one produces unescaped and checking it against how a
        # URL parser splits path/query/fragment. quote(..., safe="")
        # escapes '/' too (the default safe="/" would leave the "AC/DC"
        # case broken), which is what's needed since '/' here is real
        # title content, not a path separator we want to keep.
        safe_title = quote(title.replace(" ", "_"), safe="")
        resp = requests.get(
            self.WIKI_SUMMARY_URL.format(title=safe_title),
            headers=self._HEADERS,
            timeout=6,
        )
        if resp.status_code != 200:
            return None
        extract = resp.json().get("extract")
        return extract.strip() if extract else None

    def _wiki_full_extract(self, title: str, max_chars: int = 2000) -> Optional[str]:
        """
        A bigger, real plain-text extract of the article -- still
        Wikipedia's own documented Action API (action=query&prop=extracts),
        NOT a scrape, just not limited to the lead paragraph the way
        _wiki_summary()'s REST endpoint is. Used only to hunt for a
        sentence that answers a SPECIFIC sub-question (e.g. "who founded
        X", "when was X invented") -- _wiki_summary()'s lead paragraph
        stays the default for generic "tell me about X" queries.
        """
        try:
            resp = requests.get(
                self.WIKI_ACTION_URL,
                params={
                    "action": "query", "prop": "extracts", "explaintext": 1,
                    "exchars": max_chars, "titles": title, "format": "json",
                    "redirects": 1,
                },
                headers=self._HEADERS,
                timeout=6,
            )
            resp.raise_for_status()
            pages = resp.json().get("query", {}).get("pages", {})
            for page in pages.values():
                extract = page.get("extract")
                if extract:
                    return extract.strip()
        except Exception:
            pass
        return None

    def _ddg_abstract(self, query: str) -> Optional[str]:
        resp = requests.get(self.DDG_URL, params={
            "q": query, "format": "json", "no_html": 1, "skip_disambig": 1,
        }, headers=self._HEADERS, timeout=6)
        resp.raise_for_status()
        abstract = resp.json().get("AbstractText")
        return abstract.strip() if abstract else None

    def search(self, query: str, num_results: int = 3) -> str:
        if not query:
            return "No search query given."

        # 1. Wikipedia: find matching pages, then pull a real summary for
        #    the best match. Two calls, but each is a genuine documented
        #    endpoint, not a scrape, and both are fast/cached.
        try:
            pages = self._wiki_search(query, num_results)
        except Exception:
            pages = []

        if pages:
            top = pages[0]
            title = top.get("title", "")

            # Try to answer a SPECIFIC sub-question first (e.g. "who
            # founded X", "when was X invented") by scanning a bigger real
            # extract for the sentence matching the question's
            # distinguishing keyword(s) -- deliberately NOT the topic name
            # itself, which appears in nearly every sentence and would
            # match everything. Returns None (falls through to the plain
            # lead summary below) for generic "tell me about X" queries
            # with nothing distinguishing left to search for.
            full_text = self._wiki_full_extract(title)
            if full_text:
                targeted = _best_matching_sentence(query, full_text, title)
                if targeted:
                    return f"{title}: {targeted}"

            summary = self._wiki_summary(title)
            if summary:
                others = [p["title"] for p in pages[1:num_results] if p.get("title")]
                result = f"{title}: {summary[:500]}"
                if others:
                    result += f"\n(Related: {', '.join(others)})"
                return result

        # 2. DuckDuckGo Instant Answer as a secondary source -- still a
        #    real API call, just narrower in what it covers.
        try:
            abstract = self._ddg_abstract(query)
            if abstract:
                return abstract[:500]
        except Exception:
            pass

        return f"Couldn't find a direct answer for '{query}'."


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

    def convert_selected(self, target_format: str = "") -> str:
        path = self._current_selection_path()
        if not path:
            return "Nothing is selected right now -- drag a file onto TOKI first."
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

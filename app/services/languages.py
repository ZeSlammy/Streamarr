"""
Phase 3 — derive movie audio-language from XTREAM provider metadata.

The catalog encodes language in two reliable-ish places:
- `moviecategory.category_name` — typically `XX - <genre>` where XX is a
  language or country code (`FR - DRAME`, `EN - ACTION`, `NL - KINDEREN`).
- `movie.name` — sometimes `XX - <title>` when the category itself is
  generic (`AMAZON MOVIES`, `DISCOVERY+`).

Some prefixes encode subtitle language, not audio: `FR - VOSTFR`,
`ASIA MOVIES (MULTI-SUBS)`. Those are flagged `subs_only=True` so the
filter can opt into them separately.
"""

from __future__ import annotations

import re
from typing import Optional

# ISO 639-1 codes the provider actually uses as prefixes, plus a handful of
# country codes we remap to a language below.
_KNOWN_PREFIX_CODES = {
    # languages
    "fr", "en", "de", "it", "es", "pt", "nl", "pl", "tr", "ru", "ar",
    "ja", "ko", "zh", "hi", "bn", "el", "he", "fa", "ro", "sv", "no",
    "da", "fi", "cs", "sk", "hu", "th", "vi", "id", "ms", "uk",
    "sq", "bg", "hr", "sr", "sl", "et", "lv", "lt", "is", "ga",
    # country codes used as language hints
    "us", "gb", "ca", "au", "br", "mx", "ar", "co", "cl", "ve",
    "be", "ch", "at", "ie", "nz", "za", "jp", "kr", "cn", "hk",
    "tw", "in", "id", "pk", "ph", "sa", "ae", "eg", "il", "ir",
    "al", "bg", "ro", "hu", "cz", "sk", "rs", "hr", "si", "ee",
    "lv", "lt", "fi", "se", "no", "dk", "tr", "gr", "pt", "es",
    "it", "fr", "de", "nl", "pl", "ru", "ua", "by", "kz", "af",
    "tn", "ma", "dz", "ng", "ke", "et",
}

# Country / non-ISO codes → ISO 639-1 language code. Only entries where the
# country code differs from the dominant language code, AND where the country
# code isn't itself a valid ISO 639-1 with a *different* meaning we'd shadow.
# Notes:
# - `AR` is intentionally NOT remapped (would clobber Arabic-as-language). The
#   provider doesn't use `AR -` for Argentinian content in our catalog.
# - `BE → nl` is empirically correct for this catalog (`BE - DOCU-MOVIES (NL)`).
# - `CH → de` is the best single-language default for Swiss content.
_COUNTRY_TO_LANG: dict[str, str] = {
    "us": "en", "gb": "en", "uk": "en", "ca": "en", "au": "en",
    "nz": "en", "ie": "en", "za": "en", "ng": "en", "ke": "en",
    "br": "pt",
    "mx": "es", "co": "es", "cl": "es", "ve": "es",
    "be": "nl",
    "ch": "de", "at": "de",
    "jp": "ja",
    "kr": "ko",
    "cn": "zh", "hk": "zh", "tw": "zh",
    "in": "hi",
    "ir": "fa",
    "il": "he",
    "gr": "el",
    "sa": "ar", "ae": "ar", "eg": "ar", "tn": "ar", "ma": "ar", "dz": "ar",
    "rs": "sr",
    "by": "ru",
    "kz": "ru",
    "ua": "uk",
    "al": "sq",
    "se": "sv",
    "dk": "da",
    "pk": "ur",
    "ph": "tl",
}

# Sub markers: substring search against the lower-cased tail (everything after
# the `XX -` prefix). When any of these is present, the entry is flagged
# `subs_only=True` — original audio, our language only in the subtitle track.
_SUB_MARKERS = (
    "vostfr",
    "multi-subs",
    "multi subs",
    "sub eng",
    "sub fr",
    " sub ",
    " subs",
    "subbed",
)

# Multi-audio markers: yield `lang="multi"`, `subs_only=False`. Check before
# the sub markers so `MULTI-SUBS` falls through to the sub branch.
_MULTI_AUDIO_MARKERS = (
    "multi-audio",
    "multi audio",
    "multilang",
    "multi-lang",
)

# `XX - …` prefix parser. Allows 2–3 letter codes followed by space-dash-space
# or just dash. Captures the rest as the "tail" we scan for sub markers.
_PREFIX_RE = re.compile(r"^\s*([A-Za-z]{2,3})\s*-\s*(.*)$")


def _classify(tail_lower: str) -> tuple[bool, bool]:
    """Returns (is_multi_audio, is_subs_only). Multi wins over subs to keep
    things deterministic when both match (`MULTI - MULTI-SUBS`)."""
    if any(m in tail_lower for m in _MULTI_AUDIO_MARKERS):
        return True, False
    if any(m in tail_lower for m in _SUB_MARKERS):
        return False, True
    return False, False


def _resolve_code(raw_code: str) -> Optional[str]:
    """Lowercase + remap to a known ISO 639-1 code, or None if unrecognised."""
    code = raw_code.lower()
    if code in _COUNTRY_TO_LANG:
        return _COUNTRY_TO_LANG[code]
    if code in _KNOWN_PREFIX_CODES:
        return code
    return None


def _try_prefix(text: Optional[str]) -> Optional[tuple[str, bool, bool]]:
    """Returns (lang, is_multi, is_subs_only) when `text` carries a recognised
    `XX -` prefix, else None."""
    if not text:
        return None
    m = _PREFIX_RE.match(text)
    if not m:
        return None
    code = _resolve_code(m.group(1))
    if code is None:
        return None
    tail = m.group(2).lower()
    is_multi, is_subs = _classify(tail)
    return (code, is_multi, is_subs)


def derive_movie_language(
    category_name: Optional[str],
    title: Optional[str],
) -> tuple[Optional[str], bool, Optional[str]]:
    """Derive a movie's audio language from the strongest available signal.

    Returns `(lang, subs_only, source)`:
    - `lang`: lowercase ISO 639-1 ("fr", "en", …) or "multi", or None if no
      signal could be derived.
    - `subs_only`: True when the prefix encodes a subtitle marker rather than
      an audio language (e.g. `FR - VOSTFR`).
    - `source`: "category" | "title" | None. Useful for debugging/auditing
      derivations.

    Category prefix wins over title prefix. A category-level multi-audio /
    multi-subs marker also wins (e.g. category is `ASIA MOVIES (MULTI-SUBS)`).
    """
    for source, text in (("category", category_name), ("title", title)):
        result = _try_prefix(text)
        if result is not None:
            code, is_multi, is_subs = result
            if is_multi:
                return ("multi", False, source)
            return (code, is_subs, source)

    # Category prefix didn't parse — look for a bare MULTI-SUBS / MULTI-AUDIO
    # marker anywhere in either field.
    for source, text in (("category", category_name), ("title", title)):
        if not text:
            continue
        low = text.lower()
        if any(m in low for m in _MULTI_AUDIO_MARKERS):
            return ("multi", False, source)
        if any(m in low for m in _SUB_MARKERS):
            return ("multi", True, source)

    return (None, False, None)


def parse_allowed_languages(raw: str) -> set[str]:
    """Parse the ALLOWED_LANGUAGES csv setting into a lowercased set.
    Empty input yields an empty set (i.e. filter is disabled at this layer)."""
    if not raw:
        return set()
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def movie_language_clause(allowed: set[str], allow_unknown: bool, allow_subs_only: bool):
    """SQLAlchemy WHERE clause for the Movie model that gates on language.

    Branches OR'd together:
    - `lang IN allowed` (and `subs_only=False` unless `allow_subs_only=True`)
    - `lang IS NULL` (only when `allow_unknown=True`)

    When no branches are active (allowed empty and allow_unknown=False) we
    return a match-none clause so callers can blindly AND it into queries.
    """
    from sqlmodel import and_, or_
    from models import Movie

    branches = []
    if allowed:
        audio_branch = Movie.lang.in_(allowed)
        if not allow_subs_only:
            audio_branch = and_(audio_branch, Movie.subs_only == False)  # noqa: E712
        branches.append(audio_branch)
    if allow_unknown:
        branches.append(Movie.lang.is_(None))
    if not branches:
        return Movie.vod_id < 0  # match-none
    if len(branches) == 1:
        return branches[0]
    return or_(*branches)

"""UI translation (internationalization).

Every user-facing string in the source code is written in English and passed
through ``tr()``. Translations live as plain-text ``.po`` files (the standard
GNU gettext format - the same one tools like Poedit, Weblate or Crowdin
already understand) under ``falconsdr/locale/<lang>.po``.

No ``gettext``/``.mo``: the ``.po`` file is parsed directly, so a new
language is one text file - no ``msgfmt`` build step.

To add a new language (e.g. Slovak):
  1. Copy ``falconsdr/locale/pl.po`` to ``falconsdr/locale/sk.po``.
  2. Translate every ``msgstr "..."`` line (leave ``msgid`` alone - that's
     the original English text, used as the lookup key).
  3. Add the language to ``LANGUAGE_NAMES`` below so it shows up with a
     proper display name in Settings, instead of just its raw code.
That's the whole process - restart the app and it appears in the language
list automatically (``available_languages()`` just lists whatever ``.po``
files exist).

The web UI (falconsdr/web/static) uses the same catalogs: it fetches the
table for its language once at load (see catalog_for()) and translates
itself with the same English source strings as keys, so a language change
applies on the next page reload.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

LOCALE_DIR = Path(__file__).resolve().parent / "locale"

# AppConfig.language value meaning "follow the system/browser language".
AUTO = "auto"

# Names in the language picker; an unlisted .po still works, shown by its code.
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "pl": "Polski",
}

_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


def _unescape(raw: str) -> str:
    out = []
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == "\\" and i + 1 < len(raw):
            out.append(_ESCAPES.get(raw[i + 1], raw[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


_STRING_LINE = re.compile(r'^\s*"((?:[^"\\]|\\.)*)"\s*$')


def parse_po(text: str) -> dict[str, str]:
    """Parses ``msgid``/``msgstr`` pairs (values may continue over several
    quoted lines). A malformed line is skipped, so one bad entry can't break
    the whole language.
    """
    catalog: dict[str, str] = {}
    msgid: list[str] | None = None
    msgstr: list[str] | None = None
    target: str | None = None  # "id" or "str" - which list new string lines append to

    def _flush() -> None:
        if msgid is not None and msgstr is not None:
            key = "".join(msgid)
            value = "".join(msgstr)
            if key and value:
                catalog[key] = value

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("msgid "):
            _flush()
            msgid, msgstr, target = [], None, "id"
            m = _STRING_LINE.match(stripped[len("msgid "):])
            if m:
                msgid.append(_unescape(m.group(1)))
            continue
        if stripped.startswith("msgstr "):
            msgstr, target = [], "str"
            m = _STRING_LINE.match(stripped[len("msgstr "):])
            if m:
                msgstr.append(_unescape(m.group(1)))
            continue
        m = _STRING_LINE.match(stripped)
        if m and target == "id" and msgid is not None:
            msgid.append(_unescape(m.group(1)))
        elif m and target == "str" and msgstr is not None:
            msgstr.append(_unescape(m.group(1)))
    _flush()
    return catalog


def available_languages() -> list[str]:
    """Every language with a .po file present, plus "en" (the source
    language, needs no file - tr() just returns its input unchanged)."""
    langs = ["en"]
    if LOCALE_DIR.is_dir():
        langs += sorted(p.stem for p in LOCALE_DIR.glob("*.po"))
    return langs


def language_display_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code, code)


def resolve_system_language() -> str:
    """Maps the OS's own configured language to one of our supported codes,
    or "en" if it isn't one we have a translation for. Used when
    AppConfig.language is AUTO for server-side text (engine log lines); the
    web UI itself follows the browser's language instead (see
    web/server.py's catalog endpoint)."""
    code = system_language_code()
    return code if code in available_languages() else "en"


def system_language_code() -> str:
    """The OS's language as a two-letter ISO code, translated by this app or
    not ("" if unknown). Windows reports locale names like "Polish_Poland"
    rather than "pl_PL", so it is asked for its UI language id directly."""
    import locale
    candidates = [os.environ.get(k, "") for k in ("LC_ALL", "LC_MESSAGES", "LANG")]
    if sys.platform == "win32":
        try:
            import ctypes
            candidates.append(locale.windows_locale.get(ctypes.windll.kernel32.GetUserDefaultUILanguage(), ""))
        except (AttributeError, OSError):
            pass
    try:
        candidates.append(locale.getlocale()[0] or "")
    except ValueError:
        pass
    for code in candidates:
        if re.match(r"^[A-Za-z]{2}([_.-]|$)", code):
            return code[:2].lower()
    return ""


def catalog_for(lang: str) -> dict[str, str]:
    """The msgid -> msgstr table for ``lang`` ({} for English or an unknown
    code) - what the web UI fetches to translate itself."""
    path = LOCALE_DIR / f"{lang}.po"
    if lang == "en" or not path.exists():
        return {}
    try:
        return parse_po(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


_catalog: dict[str, str] = {}
_current_language = "en"


def set_language(lang: str) -> None:
    """Loads ``falconsdr/locale/<lang>.po`` into the active catalog. ``lang``
    of AUTO resolves via resolve_system_language() first. "en" (or any
    language with no .po file, e.g. a code that got typed wrong in
    config.json) means "no translation" - tr() then just echoes its input,
    which is already English, so this is always a safe, silent fallback."""
    global _catalog, _current_language
    if lang == AUTO:
        lang = resolve_system_language()
    _current_language = lang
    _catalog = catalog_for(lang)


def current_language() -> str:
    """The language actually in effect - already resolved past AUTO, unlike
    AppConfig.language itself, which may still literally say "auto"."""
    return _current_language


def tr(text: str) -> str:
    """Translates ``text`` (English source) to the active language, or
    returns it unchanged if there's no catalog entry - a missing/partial
    translation degrades to English for just that one string rather than
    breaking anything, so a translation file can be added to incrementally."""
    return _catalog.get(text, text)

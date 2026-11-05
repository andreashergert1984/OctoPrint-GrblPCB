"""Guard: every user-facing text is extracted and translated to German."""
import pathlib

from babel.messages.extract import extract_from_dir
from babel.messages.frontend import parse_mapping_cfg
from babel.messages.pofile import read_po

ROOT = pathlib.Path(__file__).parent.parent
BUNDLED_DE = ROOT / "octoprint_grblpcb" / "translations" / "de" / "LC_MESSAGES" / "messages.po"
SOURCE_DE = ROOT / "translations" / "de" / "LC_MESSAGES" / "messages.po"


def _source_messages():
    with open(ROOT / "babel.cfg") as f:
        method_map, options_map = parse_mapping_cfg(f)
    ids = set()
    for _file, _line, message, _comments, _context in extract_from_dir(
        str(ROOT), method_map, options_map, keywords={"gettext": None, "ngettext": (1, 2), "_": None}
    ):
        ids.add(message[0] if isinstance(message, tuple) else message)
    return ids


def _catalog(path):
    with open(path, "rb") as f:
        return read_po(f)


def test_all_source_texts_have_german_translation():
    catalog = _catalog(BUNDLED_DE)
    missing = sorted(
        msgid for msgid in _source_messages()
        if not (catalog.get(msgid) and catalog.get(msgid).string and not catalog.get(msgid).fuzzy)
    )
    assert missing == [], f"untranslated: {missing}"


def test_bundled_translation_matches_source():
    assert BUNDLED_DE.read_bytes() == SOURCE_DE.read_bytes(), (
        "run: python setup.py babel_compile && python setup.py babel_bundle --locale de"
    )

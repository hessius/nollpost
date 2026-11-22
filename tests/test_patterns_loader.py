"""Tests for declarative unsubscribe pattern loading, custom overrides, and browser locale config."""

from email.message import EmailMessage
from pathlib import Path

import pytest

from nollpost.config import load_config
from nollpost.extractor import extract_email_info
from nollpost.patterns_loader import load_unsub_patterns


def test_builtin_patterns_cover_multilingual_phrases():
    pats = load_unsub_patterns()
    assert pats.strong_unsub_re.search("Avprenumerera")
    assert pats.strong_unsub_re.search("Unsubscribe")
    assert pats.strong_unsub_re.search("Abmelden")
    assert pats.strong_unsub_re.search("Se désabonner")
    assert pats.success_text_re.search("Du är nu avregistrerad")
    assert pats.success_text_re.search("You have been unsubscribed")
    assert pats.success_text_re.search("Tilaus on peruutettu")
    assert pats.resubscribe_button_re.match("Prenumerera igen")


def test_custom_unsub_patterns_extend_defaults_and_reject_invalid_regex(tmp_path: Path):
    custom_file = tmp_path / "unsub_patterns.toml"
    custom_file.write_text(
        """
[strong_unsub]
nl = ['\\buitscrijven\\b']

[success_phrases]
nl = ['u\\s+bent\\s+uitgeschreven']
""",
        encoding="utf-8",
    )
    pats = load_unsub_patterns(custom_file)
    assert pats.strong_unsub_re.search("uitscrijven")
    assert pats.strong_unsub_re.search("unsubscribe")
    assert pats.success_text_re.search("U bent uitgeschreven")

    msg = EmailMessage()
    msg["Subject"] = "Nieuwsbrief"
    msg["From"] = "Shop <news@shop.example>"
    msg["To"] = "user@example.com"
    msg.set_content("Plain")
    msg.add_alternative("<a href='https://shop.example/stop'>Uitscrijven</a>", subtype="html")
    info = extract_email_info(msg.as_bytes(), "shop.example", patterns=pats)
    assert info.unsub_target is not None
    assert info.unsub_target.url == "https://shop.example/stop"

    bad_file = tmp_path / "bad_patterns.toml"
    bad_file.write_text("[strong_unsub]\nbad = ['[unclosed']\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid regex"):
        load_unsub_patterns(bad_file)

    flag_file = tmp_path / "flag_patterns.toml"
    flag_file.write_text("[strong_unsub]\nbad = ['(?i)uitscrijven']\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid combined regex"):
        load_unsub_patterns(flag_file)


def test_browser_locale_config_resolution(tmp_path: Path, monkeypatch):
    from nollpost.config import _default_accept_language, _detect_system_locale

    monkeypatch.setattr("nollpost.config.locale.getlocale", lambda: (None, "UTF-8"))
    monkeypatch.setenv("LANG", "UTF-8")
    assert _detect_system_locale() == "en-US"

    monkeypatch.setenv("LANG", "de_DE@euro")
    assert _detect_system_locale() == "de-DE"
    assert _default_accept_language("de-DE") == "de-DE,de;q=0.9,en;q=0.8"
    assert _default_accept_language("sv") == "sv,en;q=0.8"

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        """
[browser]
locale = "de-DE"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("NOLLPOST_CONFIG", str(cfg_path))
    monkeypatch.setenv("NOLLPOST_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NOLLPOST_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("NOLLPOST_PATTERNS", raising=False)
    cfg = load_config(ensure_files=False)
    assert cfg.browser_locale == "de-DE"
    assert cfg.accept_language == "de-DE,de;q=0.9,en;q=0.8"

    custom = tmp_path / "unsub_patterns.toml"
    custom.write_text("[strong_unsub]\nen = ['unsubscribe']\n", encoding="utf-8")
    cfg2 = load_config(ensure_files=False)
    assert cfg2.patterns_path == custom

    from nollpost.scanner import _patterns_cache_sig

    assert _patterns_cache_sig(cfg) == "builtin"
    assert _patterns_cache_sig(cfg2) != "builtin"

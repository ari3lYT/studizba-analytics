from pathlib import Path

from studizba.db import sanitize_source_html


def test_source_html_session_fields_are_redacted():
    source = """dle_login_hash = 'secret';
    <input id="si_user_id" value="123">
    <input id="si_user_hash" value="hash">
    <input id="si_p_user_key" value="key">"""
    cleaned = sanitize_source_html(source)
    assert "secret" not in cleaned
    assert 'value="123"' not in cleaned
    assert 'value="hash"' not in cleaned
    assert 'value="key"' not in cleaned
    assert cleaned.count("REDACTED") == 4


def test_committed_fixtures_do_not_contain_live_session_fields():
    fixtures = Path(__file__).parent / "fixtures"
    for path in fixtures.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert "dle_login_hash = 'REDACTED'" in text
        assert 'id="si_p_user_key" value="REDACTED"' in text

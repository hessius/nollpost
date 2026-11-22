"""Tests for MIME, multilingual unsubscribe, pertinent link, and PDF extraction."""

from email.message import EmailMessage
from pathlib import Path

from nollpost.extractor import extract_email_info, extract_pdf_attachment


def _build_html_email(
    subject: str,
    sender: str,
    html_body: str,
    headers: dict[str, str] | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = "user@example.com"
    for k, v in (headers or {}).items():
        msg[k] = v
    msg.set_content("Plain fallback")
    msg.add_alternative(html_body, subtype="html")
    return msg.as_bytes()


def test_rfc8058_header_extraction():
    raw = _build_html_email(
        "Medlemsfredag",
        "Coop <info@e.coop.se>",
        "<p>Erbjudanden</p>",
        headers={
            "List-Unsubscribe": "<https://cdn.eu1.exponea.com/prod/unsub/abc>, <mailto:unsub@exponea.com>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    )
    info = extract_email_info(raw, "e.coop.se")
    assert info.unsub_target is not None
    assert info.unsub_target.kind == "rfc8058_one_click"
    assert info.unsub_target.url == "https://cdn.eu1.exponea.com/prod/unsub/abc"
    assert info.mailto_unsub == "mailto:unsub@exponea.com"


def test_swedish_contextual_click_here_unsub():
    html = """
    <html><body>
      <a href="https://example.se/kampanj">Se erbjudanden</a>
      <a href="https://facebook.com/brand">Facebook</a>
      <a href="https://example.se/integritetspolicy">Integritetspolicy</a>
      <p>Vill du inte längre få våra utskick? <a href="https://example.se/stop?id=123">Klicka här</a></p>
    </body></html>
    """
    raw = _build_html_email("Veckans fynd", "Shop <news@example.se>", html)
    info = extract_email_info(raw, "example.se")
    assert info.unsub_target is not None
    assert info.unsub_target.kind == "https_link"
    assert info.unsub_target.url == "https://example.se/stop?id=123"
    assert info.pertinent_url == "https://example.se/kampanj"


def test_rejects_privacy_only_links():
    html = """
    <html><body>
      <p>Black Week är här! 50% rabatt!</p>
      <a href="https://shop.se/integritetspolicy">Integritetspolicy</a>
      <a href="https://shop.se/villkor">Villkor</a>
      <a href="https://instagram.com/shop">Instagram</a>
    </body></html>
    """
    raw = _build_html_email("Black Week 50% rabatt", "Shop <news@shop.se>", html)
    info = extract_email_info(raw, "shop.se")
    assert info.unsub_target is None
    assert "text:promo_or_unsub" in info.newsletter_signals


def test_consulting_guidepoint_and_coleman_links():
    html = """
    <html><body>
      <p>Consultation request #1846636</p>
      <a href="https://url.guidepointglobal.com/review">REVIEW PROJECT</a>
      <a href="https://url.guidepointglobal.com/decline">DECLINE NOW</a>
    </body></html>
    """
    raw = _build_html_email("Guidepoint - Consultation Request", "RM <rm@guidepointglobal.com>", html)
    info = extract_email_info(raw, "guidepointglobal.com")
    assert info.consulting_links.review_url == "https://url.guidepointglobal.com/review"
    assert info.consulting_links.decline_url == "https://url.guidepointglobal.com/decline"
    assert info.pertinent_url == "https://url.guidepointglobal.com/review"


def test_kivra_pertinent_link_ranked_above_footer():
    html = """
    <html><body>
      <a href="https://kivra.se/om-kivra">Om Kivra</a>
      <a href="https://app.kivra.com/inbox/msg/99">Logga in för att läsa meddelandet</a>
    </body></html>
    """
    raw = _build_html_email("Du har ett nytt meddelande i Kivra", "Kivra <noreply@notifications.kivra.com>", html)
    info = extract_email_info(raw, "Notifications.Kivra.COM", enabled_packs=["se"])
    assert info.pertinent_url == "https://app.kivra.com/inbox/msg/99"


def test_pdf_attachment_extraction(tmp_path: Path):
    msg = EmailMessage()
    msg["Subject"] = "Faktura 5819"
    msg["From"] = "Acme Billing <faktura-no-reply@billing.example.se>"
    msg["To"] = "user@example.com"
    msg.set_content("Se bifogad faktura.")
    msg.add_attachment(
        b"%PDF-1.4 sample",
        maintype="application",
        subtype="pdf",
        filename="Faktura_5819_Acme%E4%26Co.pdf",
    )
    raw = msg.as_bytes()
    info = extract_email_info(raw, "billing.example.se")
    assert len(info.pdf_attachments) == 1
    out = extract_pdf_attachment(raw, info.pdf_attachments[0].part_index, tmp_path, prefix="55552")
    assert out.exists()
    assert out.read_bytes() == b"%PDF-1.4 sample"
    assert out.parent == tmp_path


def test_unknown_charset_and_many_links_keep_footer():
    links_html = "".join(f"<a href='https://shop.se/item/{i}'>Item {i}</a>" for i in range(45))
    links_html += "<a href='https://shop.se/optout'>Avprenumerera</a>"
    raw = (
        b"From: Shop <news@shop.se>\r\n"
        b"Subject: Promo\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/alternative; boundary=\"b1\"\r\n\r\n"
        b"--b1\r\n"
        b"Content-Type: text/plain; charset=\"unknown-8bit\"\r\n\r\n"
        b"Click below to unsubscribe\r\n"
        b"--b1\r\n"
        b"Content-Type: text/html; charset=\"unknown-8bit\"\r\n\r\n"
        + f"<html><body>{links_html}</body></html>".encode("utf-8")
        + b"\r\n--b1--\r\n"
    )
    info = extract_email_info(raw, "shop.se")
    assert info.unsub_target is not None
    assert info.unsub_target.url == "https://shop.se/optout"
    assert any(l["href"] == "https://shop.se/optout" for l in info.candidate_links)


def test_pertinent_url_prefers_meticulous_blog_patreon_post_topic_and_open_in_browser():
    # 1. Meticulous wrapped tracking links: prefer /blogs/updates over /pages/links (Join the server) and homepage
    meticulous_html = """
    <html><body>
      <a href="https://track.pstmrk.it/3s/meticuloushome.com%2Fpages%2Flinks%3Fmtke%3D389/abc">Join the server</a>
      <a href="https://track.pstmrk.it/3s/meticuloushome.com%2Fblogs%2Fupdates%3Fmtke%3D389/abc">meticuloushome.com/blogs/updates</a>
      <a href="https://track.pstmrk.it/3s/meticuloushome.com%3Fmtke%3D389/abc">meticuloushome.com</a>
    </body></html>
    """
    m_info = extract_email_info(
        _build_html_email("MP4 is on the production line", "Meticulous <hello@meticuloushome.com>", meticulous_html),
        "meticuloushome.com",
        wanted_domains=["meticuloushome.com"],
    )
    assert m_info.pertinent_url is not None
    assert "blogs%2Fupdates" in m_info.pertinent_url

    # 2. Patreon "The Drip Feed - Issue 3": prefer Patreon /posts/ over embedded Wiley "A Review" article
    drip_html = """
    <html><body>
      <a href="https://www.patreon.com/jameshoffmann/posts/drip-feed-issue-174364019?utm_source=post_link">The Drip Feed - Issue 3</a>
      <a href="https://open.patreon.com/jameshoffmann/posts/drip-feed-issue-174364019">View in app</a>
      <a href="https://onlinelibrary.wiley.com/doi/10.1002/sd.71568">"The Sustainability of Robusta Coffee Under Global Change—A Review"</a>
    </body></html>
    """
    d_info = extract_email_info(
        _build_html_email("The Drip Feed - Issue 3", "James Hoffmann <jameshoffmann@creator.patreon.com>", drip_html),
        "creator.patreon.com",
        wanted_domains=["patreon.com"],
    )
    assert d_info.pertinent_url == "https://www.patreon.com/jameshoffmann/posts/drip-feed-issue-174364019?utm_source=post_link"

    # 3. Home Assistant Discourse: prefer Visit Topic (/t/...) over author profile (/u/magicus)
    ha_html = """
    <html><body>
      <a href="https://community.home-assistant.io/u/magicus">magicus</a>
      <a href="https://community.home-assistant.io/t/custom-swedish-wake-word/798623/4">Visit Topic</a>
    </body></html>
    """
    h_info = extract_email_info(
        _build_html_email("[Home Assistant] Custom Swedish Wake Word", "User <noreply@community.home-assistant.io>", ha_html),
        "community.home-assistant.io",
        wanted_domains=["home-assistant.io"],
    )
    assert h_info.pertinent_url == "https://community.home-assistant.io/t/custom-swedish-wake-word/798623/4"

    # 4. Explicit "Open in browser" / "View in browser" links must NOT be stripped and should beat generic site links
    browser_html = """
    <html><body>
      <a href="https://example.org/">Main Website</a>
      <a href="https://example.org/edition/issue-12">View in browser</a>
    </body></html>
    """
    b_info = extract_email_info(_build_html_email("Weekly Digest", "Author <hello@example.org>", browser_html), "example.org")
    assert b_info.pertinent_url == "https://example.org/edition/issue-12"

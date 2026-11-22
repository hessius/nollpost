"""Tier 1 RFC 8058 HTTP POST and Tier 2 Playwright Chromium unsubscribe/decline runner."""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

import httpx
from playwright.async_api import Page, Route, async_playwright

from nollpost.models import PlaywrightOutcome

CAPTCHA_PATTERNS = re.compile(
    r"(?:g-recaptcha|h-captcha|hcaptcha\.com|recaptcha/api|challenges\.cloudflare\.com/turnstile|cf-turnstile|verify\s+you\s+are\s+human|are\s+you\s+human|jag\s+är\s+inte\s+en\s+robot|i\s+am\s+not\s+a\s+robot)",
    re.I,
)

SUCCESS_TEXT_RE = re.compile(
    r"(?:"
    # English
    r"you(?:'ve|\s+have|\s+are)\s+(?:now\s+)?(?:successfully\s+)?(?:been\s+)?(?:unsubscribed|opted\s+out|removed)|"
    r"successfully\s+(?:been\s+)?(?:unsubscribed|opted\s+out|removed|updated)|"
    r"unsubscription\s+(?:is\s+)?(?:confirmed|complete[d]?|successful)|"
    r"removed\s+from\s+(?:our|this|the|all)\s+(?:mailing\s+|email\s+)?lists?|"
    r"no\s+longer\s+receive|"
    r"will\s+not\s+receive\s+any\s+more|"
    r"(?:email\s+|communication\s+|subscription\s+|notification\s+)?preferences\s+(?:have\s+been\s+|are\s+)?(?:saved|updated|changed)|"
    r"your\s+(?:changes|choices|settings)\s+have\s+been\s+saved|"
    # Swedish
    r"du\s+(?:är|har)\s+(?:nu\s+)?(?:blivit\s+)?(?:avregistrerad|avprenumererad|avprenumererat|borttagen)|"
    r"(?:din\s+)?(?:avprenumeration|avregistrering)\s+(?:är\s+|har\s+)?(?:nu\s+)?(?:bekräftad|genomförd|genomförts|klar|sparad|registrerad)|"
    r"(?:du\s+)?(?:avslutar|har\s+avslutat)\s+(?:din\s+)?prenumeration(?:en)?|"
    r"du\s+kommer\s+inte\s+längre\s+(?:att\s+)?(?:få|ta\s+emot|motta)|"
    r"vi\s+har\s+(?:nu\s+)?(?:tagit\s+bort\s+din|avregistrerat\s+dig|sparat\s+dina\s+val|uppdaterat\s+dina)|"
    r"(?:tack[!,]?\s*)?(?:dina\s+|din\s+|ditt\s+)?(?:inställningar|preferenser|val|ändringar|prenumeration(?:er)?)\s+(?:är\s+|har\s+)?(?:nu\s+)?(?:sparade|sparat|sparats|uppdaterade|uppdaterad|uppdaterats|ändrade|ändrats)|"
    r"alla\s+(?:e-?postutskick|prenumerationer)\s+(?:är\s+|har\s+)?(?:nu\s+)?(?:avregistrerade|avslutade|avbrutna)|"
    # Nordic / European (DA, NO, DE, FR, ES, FI)
    r"du\s+er\s+(?:nu\s+|nå\s+)?(?:afmeldt|avmeldt)|"
    r"(?:din\s+)?(?:afmelding|avmelding)\s+er\s+(?:bekræftet|registrert|gjennomført)|"
    r"(?:dine\s+|din\s+)?(?:præferencer|preferanser|indstillinger|innstillinger|abonnement(?:er)?)\s+(?:er\s+|har\s+blitt\s+)?(?:nu\s+|nå\s+)?(?:gemt|opdateret|lagret|oppdatert)|"
    r"(?:erfolgreich|wurden)\s+abgemeldet|"
    r"abmeldung\s+(?:war\s+)?(?:erfolgreich|bestätigt)|"
    r"(?:ihre\s+)?(?:einstellungen|präferenzen)\s+wurden\s+(?:gespeichert|aktualisiert)|"
    r"vous\s+(?:êtes|avez\s+été)\s+désabonné|"
    r"désabonnement\s+(?:confirmé|pris\s+en\s+compte)|"
    r"(?:vos\s+)?préférences\s+ont\s+été\s+(?:enregistrées|mises\s+à\s+jour)|"
    r"te\s+has\s+dado\s+de\s+baja|"
    r"baja\s+(?:ha\s+sido\s+)?confirmada|"
    r"(?:tus\s+|sus\s+)?preferencias\s+(?:se\s+han|han\s+sido)\s+(?:guardadas|actualizadas)|"
    r"tilaus\s+on\s+peruutettu|"
    # Consulting declines
    r"project\s+declined|"
    r"thank\s+you\s+for\s+(?:your\s+response|letting\s+us\s+know)|"
    r"you\s+have\s+declined"
    r")",
    re.I,
)

RESUBSCRIBE_BUTTON_RE = re.compile(
    r"^\s*[+↺]?\s*(?:resubscribe|re-subscribe|subscribe\s+again|prenumerera\s+igen|återaktivera(?:\s+prenumeration(?:en)?)?)\s*$",
    re.I,
)

UNSUB_BUTTON_PATTERNS = [
    # Tier 1: Explicit "unsubscribe / opt out from ALL" buttons (e.g. "Avregistrera alla e-postutskick")
    r"unsubscribe\s+(?:me\s+)?(?:from\s+)?all",
    r"opt[\s-]?out\s+(?:of\s+)?all",
    r"avprenumerera\s+(?:mig\s+)?(?:från\s+)?(?:alla|allt)",
    r"avregistrera\s+(?:mig\s+)?(?:från\s+)?(?:alla|allt)",
    r"avbryt\s+alla\s+prenumerationer",
    r"sluta\s+(?:få|ta\s+emot)\s+alla",
    r"afmeld\s+(?:mig\s+)?alle",
    r"avmeld\s+(?:meg\s+)?alle",
    r"(?:von\s+allen|alle)\s+abmelden",
    r"(?:me\s+)?désabonner\s+de\s+tout|tout\s+désabonner",
    r"dar(?:me)?\s+de\s+baja\s+de\s+todo",
    # Tier 2: Explicit confirm / unsubscribe actions
    r"bekräfta\s+(?:avprenumeration|avregistrering)",
    r"confirm\s+(?:unsubscription|unsubscribe|opt[\s-]?out)",
    r"avprenumerera",
    r"avregistrera",
    r"avsluta\s+(?:min\s+|din\s+)?prenumeration(?:en)?",
    r"sluta\s+prenumerera",
    r"ja,?\s+(?:avregistrera|avprenumerera|ta\s+bort|avsluta)",
    r"yes,?\s+(?:unsubscribe|remove|opt[\s-]?out)",
    r"unsubscribe",
    r"opt[\s-]?out",
    r"afmeld",
    r"avmeld",
    r"abmelden",
    r"(?:me\s+|se\s+)?désabonner",
    r"dar(?:me|se)?\s+de\s+baja",
    r"peruuta(?:\s+tilaus)?",
    # Tier 3: Save / update preference center choices (e.g. "Spara val", "Uppdatera")
    r"spara\s+(?:mina\s+)?(?:val|inställningar|ändringar)",
    r"save\s+(?:my\s+)?(?:preferences|changes|settings|choices)",
    r"update\s+(?:my\s+)?(?:preferences|subscription|settings)",
    r"uppdatera(?:\s+(?:inställningar|prenumeration(?:er)?|val))?",
    r"bekräfta",
    r"spara",
    r"confirm",
    r"update",
    r"submit",
    r"skicka",
    r"verkställ",
]

OPTOUT_CHOICE_PATTERNS = [
    r"unsubscribe\s+(?:me\s+)?(?:from\s+)?all",
    r"opt[\s-]?out\s+(?:of\s+)?all",
    r"avprenumerera",
    r"avregistrera",
    r"avbryt\s+alla",
    r"vill\s+inte\s+längre\s+(?:ta\s+emot|ha|få)",
    r"inga\s+fler\s+(?:utskick|mejl|publikationer|nyhetsbrev)",
    r"no\s+longer\s+(?:wish|want)\s+to\s+receive",
    r"do\s+not\s+send\s+me",
    r"stop\s+receiving\s+all",
    r"remove\s+me\s+from\s+all",
    r"afmeld\s+alle|avmeld\s+alle|alle\s+abmelden|tout\s+désabonner",
]

DECLINE_BUTTON_PATTERNS = [
    r"decline(?:\s+(?:now|project|consultation))?",
    r"not\s+my\s+area",
    r"outside\s+my\s+expertise",
    r"not\s+relevant",
    r"not\s+interested",
    r"avböj",
    r"confirm",
    r"submit",
]

_BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "metadata.google.internal"})


def _is_http_scheme(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _ip_is_non_public(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return _ip_is_non_public(ip.ipv4_mapped)
    return (
        (not ip.is_global)
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def is_public_http_url(url: str, allow_private: bool = False) -> bool:
    """Validate HTTP(S) URL and block loopback, LAN, CGNAT/non-global, and obfuscated IPv4 endpoints unless allow_private=True."""
    if not _is_http_scheme(url):
        return False
    if allow_private:
        return True
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or host in _BLOCKED_HOSTNAMES or host.endswith((".local", ".internal", ".localhost")):
        return False

    try:
        ip = ipaddress.ip_address(host)
        return not _ip_is_non_public(ip)
    except ValueError:
        # Reject octal/hex/single-integer IPv4 aliases (e.g. 0177.0.0.1, 0x7f000001, 2130706433)
        # that OS resolvers or Chromium normalize to loopback/private addresses.
        if all(re.fullmatch(r"(?:\d+|0x[0-9a-f]+)", lbl) for lbl in host.split(".")):
            return False

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Unresolvable host; let HTTP client fail cleanly rather than crashing, or reject? Rejecting SSRF only if resolves non-public,
        # wait: fail closed if DNS doesn't resolve or let network call fail? Failing closed prevents DNS rebinding/weirdness safely.
        return False

    if not infos:
        return False
    for info in infos:
        addr_str = info[4][0].split("%")[0]
        try:
            ip = ipaddress.ip_address(addr_str)
            if _ip_is_non_public(ip):
                return False
        except ValueError:
            return False
    return True


async def try_rfc8058_one_click(
    url: str,
    timeout: float = 15.0,
    allow_private: bool = False,
) -> PlaywrightOutcome:
    """Attempt RFC 8058 one-click POST unsubscription with per-hop SSRF validation."""
    if not is_public_http_url(url, allow_private=allow_private):
        return PlaywrightOutcome(status="failed", final_url=url, detail="Blocked non-public or invalid URL")

    current_url = url
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client:
            for _hop in range(5):
                resp = await client.post(
                    current_url,
                    data={"List-Unsubscribe": "One-Click"},
                    headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
                )
                if resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("location", "").strip()
                    if not loc:
                        break
                    next_url = urljoin(current_url, loc)
                    if not is_public_http_url(next_url, allow_private=allow_private):
                        return PlaywrightOutcome(
                            status="failed",
                            final_url=current_url,
                            detail="Blocked redirect to non-public or invalid URL",
                        )
                    current_url = next_url
                    continue

                body = resp.text or ""
                if CAPTCHA_PATTERNS.search(body):
                    return PlaywrightOutcome(
                        status="blocked_captcha",
                        final_url=str(resp.url),
                        detail="RFC 8058 response returned a CAPTCHA/bot gate",
                    )
                if 200 <= resp.status_code < 300:
                    if "<form" in body.lower() and re.search(r"<button|<input[^>]+submit", body, re.I) and not SUCCESS_TEXT_RE.search(body):
                        return PlaywrightOutcome(
                            status="failed",
                            final_url=str(resp.url),
                            detail="RFC 8058 returned interactive form; escalating to Playwright",
                        )
                    return PlaywrightOutcome(
                        status="success",
                        final_url=str(resp.url),
                        detail=f"RFC 8058 One-Click POST succeeded (HTTP {resp.status_code})",
                    )
                return PlaywrightOutcome(
                    status="failed",
                    final_url=str(resp.url),
                    detail=f"RFC 8058 POST returned HTTP {resp.status_code}",
                )
            return PlaywrightOutcome(
                status="failed",
                final_url=current_url,
                detail="Too many redirects during RFC 8058 POST",
            )
    except Exception as exc:
        return PlaywrightOutcome(
            status="failed",
            final_url=current_url,
            detail=f"RFC 8058 POST error ({type(exc).__name__}): {exc}",
        )


async def _detect_captcha(page: Page) -> bool:
    for frame in page.frames:
        if CAPTCHA_PATTERNS.search(frame.url or ""):
            return True
        try:
            html = await frame.content()
            if CAPTCHA_PATTERNS.search(html):
                return True
        except Exception:
            continue
    return False


async def _visible_text(page: Page) -> str:
    parts: list[str] = []
    for frame in page.frames:
        try:
            txt = await frame.evaluate("() => document.body ? document.body.innerText : ''")
            if txt and txt.strip():
                parts.append(txt.strip())
        except Exception:
            continue
    return "\n".join(parts)


async def _fill_email_if_needed(page: Page, user_email: str) -> bool:
    if not user_email:
        return False
    filled = False
    for frame in page.frames:
        inputs = frame.locator(
            "input[type='email'], input[name*='mail' i], input[id*='mail' i], input[placeholder*='mail' i]"
        )
        try:
            count = await inputs.count()
        except Exception:
            continue
        for i in range(min(count, 4)):
            loc = inputs.nth(i)
            try:
                if await loc.is_visible() and await loc.is_editable():
                    val = (await loc.input_value()).strip()
                    if not val:
                        await loc.fill(user_email)
                        filled = True
            except Exception:
                continue
    return filled


async def _uncheck_marketing_checkboxes(page: Page) -> int:
    changed = 0
    optout_guard = re.compile("|".join(OPTOUT_CHOICE_PATTERNS) + r"|all\s+email", re.I)
    for frame in page.frames:
        boxes = frame.locator("input[type='checkbox']")
        try:
            count = await boxes.count()
        except Exception:
            continue
        for i in range(min(count, 25)):
            loc = boxes.nth(i)
            try:
                if await loc.is_visible() and await loc.is_checked():
                    label_txt = await loc.evaluate(
                        """el => {
                            const lbl = (el.labels && el.labels[0]) || el.closest('label, tr, li, div, p');
                            return (lbl ? lbl.innerText : el.name || el.value || '').toLowerCase();
                        }"""
                    )
                    if optout_guard.search(label_txt):
                        continue
                    await loc.uncheck()
                    changed += 1
            except Exception:
                continue
    return changed


async def _mark_optout_container(loc) -> None:
    try:
        await loc.evaluate(
            """el => {
                const container = el.closest('form, fieldset, section') || el.closest('table, div');
                if (container) container.setAttribute('data-triage-optout-form', '1');
            }"""
        )
    except Exception:
        pass


async def _check_optout_radios_or_boxes(page: Page, patterns: list[str]) -> bool:
    """Select radio/checkbox options matching opt-out/decline reasons across all frames."""
    combined_re = re.compile("|".join(patterns), re.I)

    for frame in page.frames:
        labels = frame.locator("label")
        try:
            count = await labels.count()
        except Exception:
            continue
        for i in range(min(count, 30)):
            lbl = labels.nth(i)
            try:
                txt = (await lbl.inner_text()).strip()
                if combined_re.search(txt) and await lbl.is_visible():
                    for_attr = await lbl.get_attribute("for")
                    if for_attr:
                        target = frame.locator(f"id={for_attr}")
                        if await target.count() > 0:
                            if not await target.first.is_checked():
                                await lbl.click()
                            await _mark_optout_container(target.first)
                            return True
                    inp = lbl.locator("input[type='radio'], input[type='checkbox']")
                    if await inp.count() > 0:
                        if not await inp.first.is_checked():
                            await lbl.click()
                        await _mark_optout_container(inp.first)
                        return True
            except Exception:
                continue

        inputs = frame.locator("input[type='checkbox'], input[type='radio']")
        try:
            icount = await inputs.count()
        except Exception:
            continue
        for i in range(min(icount, 30)):
            inp = inputs.nth(i)
            try:
                if not await inp.is_visible():
                    continue
                context_txt = await inp.evaluate(
                    """el => {
                        const lbl = (el.labels && el.labels[0]) || el.closest('label, tr, li, p, div');
                        return (lbl ? lbl.innerText : el.name || el.value || '').trim();
                    }"""
                )
                if context_txt and len(context_txt) <= 240 and combined_re.search(context_txt):
                    if not await inp.is_checked():
                        await inp.check()
                    await _mark_optout_container(inp)
                    return True
            except Exception:
                continue
    return False


async def _has_resubscribe_button(page: Page) -> bool:
    selectors = "button, input[type='submit'], input[type='button'], [role='button'], a"
    for frame in page.frames:
        candidates = frame.locator(selectors)
        try:
            count = await candidates.count()
        except Exception:
            continue
        for i in range(min(count, 40)):
            loc = candidates.nth(i)
            try:
                if not await loc.is_visible():
                    continue
                tag = await loc.evaluate("el => el.tagName.toLowerCase()")
                text = (
                    (await loc.get_attribute("value") or "")
                    if tag == "input"
                    else (await loc.inner_text() or "")
                ).strip()
                if text and RESUBSCRIBE_BUTTON_RE.match(text):
                    return True
            except Exception:
                continue
    return False


async def _click_best_action(
    page: Page,
    patterns: list[str],
    seen_signatures: set[str] | None = None,
) -> str | None:
    selectors = "button, input[type='submit'], input[type='button'], [role='button'], a"
    elements: list[tuple[int, int, int, Any, str, str]] = []
    for f_idx, frame in enumerate(page.frames):
        candidates = frame.locator(selectors)
        try:
            count = await candidates.count()
        except Exception:
            continue
        for i in range(min(count, 90)):
            loc = candidates.nth(i)
            try:
                if not await loc.is_visible():
                    continue
                info = await loc.evaluate(
                    """el => {
                        const tag = el.tagName.toLowerCase();
                        const href = (el.getAttribute('href') || '').trim();
                        const role = (el.getAttribute('role') || '').toLowerCase();
                        const hasOnclick = Boolean(el.getAttribute('onclick'));
                        const inOptoutForm = Boolean(el.closest('[data-triage-optout-form=\"1\"]'));
                        const aria = (el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
                        return { tag, href, role, hasOnclick, inOptoutForm, aria };
                    }"""
                )
                tag = info["tag"]
                href = info["href"]
                # Skip in-page anchor jump links (<a href="#...">) or mailto/tel links that do not submit
                if tag == "a" and info["role"] != "button" and not info["hasOnclick"]:
                    if not href or href.startswith("#") or href.lower().startswith(("mailto:", "tel:")):
                        continue
                if tag == "input":
                    text = (await loc.get_attribute("value") or info["aria"] or "").strip()
                else:
                    text = ((await loc.inner_text()) or info["aria"] or "").strip()
                text = re.sub(r"\s+", " ", text)
                if not text or len(text) > 100:
                    continue
                if RESUBSCRIBE_BUTTON_RE.match(text):
                    continue
                if re.search(r"privacy|integritet|cookie|terms|villkor|login|sign\s+in|logga\s+in", text, re.I):
                    continue
                sig = f"{page.url}::f{f_idx}::{tag}::{i}::{text.lower()}"
                if seen_signatures is not None and sig in seen_signatures:
                    continue
                for rank, pat in enumerate(patterns):
                    if re.search(pat, text, re.I):
                        tag_penalty = 0 if tag in ("button", "input") or info["role"] == "button" else 4
                        form_bonus = -5 if info["inOptoutForm"] else 0
                        weight = rank * 10 + tag_penalty + form_bonus
                        elements.append((weight, f_idx, i, loc, text, sig))
                        break
            except Exception:
                continue

    if not elements:
        return None
    elements.sort(key=lambda x: (x[0], x[1], x[2]))
    _, _, _, chosen_loc, matched_text, chosen_sig = elements[0]
    if seen_signatures is not None:
        seen_signatures.add(chosen_sig)
    await chosen_loc.click(timeout=5000)
    return matched_text


async def run_playwright_action(
    url: str,
    *,
    mode: Literal["unsubscribe", "consulting_decline"] = "unsubscribe",
    user_email: str = "",
    screenshots_dir: Path,
    headless: bool = True,
    max_steps: int = 6,
    allow_private: bool = False,
) -> PlaywrightOutcome:
    """Execute deterministic Playwright navigation & clicks to confirm unsub/decline."""
    if not is_public_http_url(url, allow_private=allow_private):
        return PlaywrightOutcome(status="failed", final_url=url, detail="Blocked non-public or invalid URL")

    screenshots_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time() * 1000)
    shot_path = screenshots_dir / f"{mode}-{stamp}.png"

    patterns = DECLINE_BUTTON_PATTERNS if mode == "consulting_decline" else UNSUB_BUTTON_PATTERNS
    clicked_history: list[str] = []
    seen_signatures: set[str] = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": 1200, "height": 900},
            locale="sv-SE",
            extra_http_headers={"Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8"},
        )

        async def _route_guard(route: Route) -> None:
            req_url = route.request.url
            if (
                req_url == "about:blank"
                or req_url.startswith(("data:", "blob:"))
                or is_public_http_url(req_url, allow_private=allow_private)
            ):
                await route.continue_()
            else:
                await route.abort("blockedbyclient")

        await context.route("**/*", _route_guard)
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass

            for _step in range(max_steps):
                if await _detect_captcha(page):
                    await page.screenshot(path=str(shot_path), full_page=False)
                    return PlaywrightOutcome(
                        status="blocked_captcha",
                        final_url=page.url if _is_http_scheme(page.url) else url,
                        detail="CAPTCHA/anti-bot verification detected — paused for manual browser click",
                        screenshot_path=str(shot_path),
                    )

                vis_text = await _visible_text(page)
                if SUCCESS_TEXT_RE.search(vis_text) or (mode == "unsubscribe" and await _has_resubscribe_button(page)):
                    await page.screenshot(path=str(shot_path), full_page=False)
                    return PlaywrightOutcome(
                        status="success",
                        final_url=page.url if _is_http_scheme(page.url) else url,
                        detail=f"Confirmed on page ({' -> '.join(clicked_history) or 'immediate load'})",
                        screenshot_path=str(shot_path),
                    )

                await _fill_email_if_needed(page, user_email)
                if mode == "unsubscribe":
                    await _check_optout_radios_or_boxes(page, OPTOUT_CHOICE_PATTERNS)
                    await _uncheck_marketing_checkboxes(page)
                else:
                    await _check_optout_radios_or_boxes(
                        page,
                        [r"not\s+my\s+area", r"outside\s+my\s+expertise", r"not\s+relevant", r"not\s+interested"],
                    )

                clicked = await _click_best_action(page, patterns, seen_signatures=seen_signatures)
                if not clicked:
                    break
                clicked_history.append(clicked)
                await page.wait_for_timeout(1500)
                try:
                    await page.wait_for_load_state("networkidle", timeout=4000)
                except Exception:
                    pass

            if await _detect_captcha(page):
                await page.screenshot(path=str(shot_path), full_page=False)
                return PlaywrightOutcome(
                    status="blocked_captcha",
                    final_url=page.url if _is_http_scheme(page.url) else url,
                    detail="CAPTCHA detected after submit — manual completion required",
                    screenshot_path=str(shot_path),
                )

            vis_text = await _visible_text(page)
            await page.screenshot(path=str(shot_path), full_page=False)
            if SUCCESS_TEXT_RE.search(vis_text) or (mode == "unsubscribe" and await _has_resubscribe_button(page)):
                return PlaywrightOutcome(
                    status="success",
                    final_url=page.url if _is_http_scheme(page.url) else url,
                    detail=f"Confirmed after clicking {' -> '.join(clicked_history)}",
                    screenshot_path=str(shot_path),
                )

            dom_snap = (await page.content())[:6000]
            return PlaywrightOutcome(
                status="failed",
                final_url=page.url if _is_http_scheme(page.url) else url,
                detail=f"No confirmation phrase found after steps ({' -> '.join(clicked_history) or 'no matching button'})",
                screenshot_path=str(shot_path),
                dom_snapshot=dom_snap,
            )
        except Exception as exc:
            try:
                await page.screenshot(path=str(shot_path), full_page=False)
            except Exception:
                shot_path = None  # type: ignore[assignment]
            safe_final = page.url if _is_http_scheme(page.url) else url
            return PlaywrightOutcome(
                status="failed",
                final_url=safe_final,
                detail=f"Playwright error: {exc}",
                screenshot_path=str(shot_path) if shot_path else None,
            )
        finally:
            await context.close()
            await browser.close()

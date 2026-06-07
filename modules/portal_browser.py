"""Headless-browser captive-portal fallback using Playwright + Chromium.

This is the last-resort strategy for portals the lightweight HTTP path can't
solve: JS-only portals (a ``<button onclick>`` / ``<a>`` firing an XHR with no
real ``<form>``), and simple tickbox/single-click splash pages whose buttons
the stdlib HTMLParser path mishandles.

Egress / interface-binding limitation
--------------------------------------
The existing HTTP path binds its source IP to the connected WiFi card via
``_SourceAddressAdapter``. A headless browser process can't be told to bind a
particular source IP the same way. In the common captive-portal case this is
fine: the portal host is the directly-reachable gateway on the connected
interface's own subnet, so a plain headless browse reaches it while we're
connected on that card. This is therefore a best-effort approach.

TODO: for true per-interface egress (multiple cards connected at once, or a
portal reached off-subnet) launch Chromium inside a network namespace bound to
the interface, or route its traffic via the interface's policy-routing table.
That plumbing is intentionally omitted here to keep this simple.

Graceful degradation
---------------------
If Playwright isn't installed, or Chromium isn't installed/launchable, every
entry point catches the error, logs a warning, and returns False so the HTTP
path still governs the outcome. The module always imports.
"""

import os
import re

from logging_config import get_logger

logger = get_logger('portal_browser')

# Where the bundled Chromium lives. The installer (deploy.sh) and the systemd
# unit pin PLAYWRIGHT_BROWSERS_PATH to "<app-dir>/.playwright" so install-time
# and runtime agree even when the service runs as root with a different $HOME.
# If nothing set it (e.g. a manual `python vasili.py`), default to a path next
# to the repo so the browse still finds Chromium.
if not os.environ.get('PLAYWRIGHT_BROWSERS_PATH'):
    _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _bundled = os.path.join(_repo_root, '.playwright')
    if os.path.isdir(_bundled):
        os.environ['PLAYWRIGHT_BROWSERS_PATH'] = _bundled

# Text/value patterns for the "accept / continue" control on a portal.
_ACCEPT_PATTERN = re.compile(
    r'accept|agree|continue|connect|free|start|online|i ?agree|proceed|enter',
    re.I,
)


def solve_with_browser(redirect_url: str, interface: str = None,
                       timeout: int = 30) -> bool:
    """Drive a headless Chromium to clear a captive portal.

    Navigates to ``redirect_url``, ticks all visible checkboxes, clicks the
    most likely accept control, waits for the network to settle, then confirms
    success with a real connectivity recheck bound to ``interface``.

    Args:
        redirect_url: The portal URL to open.
        interface: WiFi interface to verify connectivity through. If None,
            connectivity can't be verified and we return False (the HTTP path
            already had its chance).
        timeout: Overall budget in seconds for navigation/interaction.

    Returns:
        True only if connectivity is verified afterwards; False otherwise
        (including when Playwright/Chromium are unavailable).
    """
    if not redirect_url:
        logger.debug('No redirect URL for browser fallback')
        return False

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        logger.warning(
            f'Playwright not available — browser fallback disabled ({e}). '
            f'Install with: pip install playwright && playwright install chromium'
        )
        return False

    timeout_ms = max(1, timeout) * 1000

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as e:
                logger.warning(
                    f'Could not launch Chromium — browser fallback disabled ({e}). '
                    f'Run: playwright install chromium (plus arm64 system deps)'
                )
                return False

            try:
                context = browser.new_context(
                    user_agent=(
                        'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 '
                        'Chrome/125.0 Mobile Safari/537.36'
                    ),
                    ignore_https_errors=True,
                )
                page = context.new_page()
                page.set_default_timeout(timeout_ms)

                logger.info(f'Browser fallback navigating to {redirect_url}')
                page.goto(redirect_url, wait_until='domcontentloaded',
                          timeout=timeout_ms)

                _tick_checkboxes(page)
                _click_accept(page)
                _wait_idle(page, timeout_ms)
            except Exception as e:
                logger.debug(f'Browser interaction error: {str(e)[:200]}')
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f'Browser fallback failed: {str(e)[:200]}')
        return False

    # Success is decided by a real connectivity recheck, never by page state.
    if not interface:
        logger.debug('No interface to verify browser-fallback connectivity')
        return False
    try:
        import network_isolation
        ok = network_isolation.verify_connectivity(interface)
        logger.info(f'Browser fallback connectivity on {interface}: {ok}')
        return ok
    except Exception as e:
        logger.debug(f'Connectivity recheck after browser failed: {e}')
        return False


def _tick_checkboxes(page) -> None:
    """Check every visible, enabled checkbox (terms/marketing/etc.)."""
    try:
        boxes = page.query_selector_all('input[type="checkbox"]')
    except Exception:
        return
    for box in boxes:
        try:
            if box.is_visible() and box.is_enabled() and not box.is_checked():
                box.check(force=True)
        except Exception:
            # Best-effort; some boxes are decorative or detached.
            continue


def _click_accept(page) -> bool:
    """Click the most likely accept/continue control. Returns True if clicked."""
    selectors = [
        'button',
        'input[type="submit"]',
        'input[type="button"]',
        'a',
        '[role="button"]',
    ]
    for selector in selectors:
        try:
            elements = page.query_selector_all(selector)
        except Exception:
            continue
        for el in elements:
            try:
                if not (el.is_visible() and el.is_enabled()):
                    continue
                label = (el.inner_text() or '').strip()
                if not label:
                    # Fall back to value / aria-label attributes.
                    label = (el.get_attribute('value')
                             or el.get_attribute('aria-label') or '')
                if label and _ACCEPT_PATTERN.search(label):
                    el.click(force=True)
                    logger.info(f'Browser fallback clicked: {label[:40]!r}')
                    return True
            except Exception:
                continue

    # No labelled match — click the first visible submit as a last resort.
    try:
        submit = page.query_selector('input[type="submit"], button[type="submit"]')
        if submit and submit.is_visible():
            submit.click(force=True)
            logger.info('Browser fallback clicked first submit control')
            return True
    except Exception:
        pass
    return False


def _wait_idle(page, timeout_ms: int) -> None:
    """Wait for the network to settle after submitting."""
    try:
        page.wait_for_load_state('networkidle', timeout=timeout_ms)
    except Exception:
        # networkidle can time out on long-polling portals; that's fine.
        pass

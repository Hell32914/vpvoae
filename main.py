import asyncio
import os
import sys
import logging
import math
import random
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from datetime import datetime
from urllib.parse import urljoin, urlparse
from playwright.async_api import async_playwright

# Конфигурация логирования для продакшена
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(value, max_value))


def cubic_bezier(t: float, p0: float, p1: float, p2: float, p3: float) -> float:
    mt = 1 - t
    return (mt ** 3) * p0 + 3 * (mt ** 2) * t * p1 + 3 * mt * (t ** 2) * p2 + (t ** 3) * p3


async def collect_interactive_targets(
    page: Any,
    viewport_width: int,
    viewport_height: int,
    limit: int,
) -> List[Dict[str, Any]]:
    """Собирает видимые интерактивные DOM-элементы в текущем viewport."""
    return await page.evaluate(
        """
        ({ limit, viewportWidth, viewportHeight }) => {
            const selectors = [
                'a[href]',
                'button',
                'input:not([type="hidden"])',
                'select',
                'textarea',
                '[role="button"]',
                '[role="link"]',
                '[role="menuitem"]',
                '[onclick]',
                '[tabindex]:not([tabindex="-1"])',
                '[contenteditable="true"]',
                '[aria-controls]',
                '[data-action]',
                '[data-hover]',
                '[class*="btn"]',
                '[class*="link"]',
                '[class*="card"]',
                '[class*="tile"]',
                '[aria-haspopup="true"]',
                'video',
                'canvas',
                'summary'
            ];

            const pool = new Set();

            function addIfElement(node) {
                if (node && node instanceof HTMLElement) {
                    pool.add(node);
                }
            }

            function getClickable(node) {
                if (!node || !(node instanceof Element)) return null;
                const clickable = node.closest('a,button,input,select,textarea,summary,[role="button"],[role="link"],[role="menuitem"],[onclick],[tabindex]:not([tabindex="-1"]),[contenteditable="true"],[aria-controls],[data-action]');
                if (clickable && clickable instanceof HTMLElement) return clickable;

                if (node instanceof HTMLElement) {
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    const hasSize = rect.width >= 14 && rect.height >= 14;
                    if (hasSize && (style.cursor === 'pointer' || node.tagName.toLowerCase() === 'canvas' || node.tagName.toLowerCase() === 'video')) {
                        return node;
                    }
                }

                return null;
            }

            function elementKey(el, cx, cy, text) {
                const parts = [];
                let cur = el;
                let depth = 0;
                while (cur && cur instanceof HTMLElement && depth < 5) {
                    const cls = (cur.className || '').toString().trim().split(/\\s+/).slice(0, 2).join('.');
                    parts.push(`${cur.tagName.toLowerCase()}${cur.id ? '#' + cur.id : ''}${cls ? '.' + cls : ''}`);
                    cur = cur.parentElement;
                    depth += 1;
                }

                return `${parts.join('>')}|${Math.round(cx)}:${Math.round(cy)}|${text.slice(0, 30)}`;
            }

            for (const node of document.querySelectorAll(selectors.join(','))) {
                addIfElement(node);
            }

            // Grid scan помогает находить интерактивы на нестандартной верстке и canvas/UI-оверлеях.
            const cols = 8;
            const rows = 5;
            for (let r = 0; r < rows; r++) {
                for (let c = 0; c < cols; c++) {
                    const x = ((c + 0.5) / cols) * viewportWidth;
                    const y = ((r + 0.5) / rows) * viewportHeight;
                    const stack = document.elementsFromPoint(x, y) || [];
                    for (const node of stack.slice(0, 6)) {
                        const clickable = getClickable(node);
                        if (clickable) addIfElement(clickable);
                    }
                }
            }

            const out = [];
            const diag = Math.hypot(viewportWidth, viewportHeight);

            for (const el of pool) {
                if (!(el instanceof HTMLElement)) continue;

                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || '1') < 0.05) {
                    continue;
                }
                if (style.pointerEvents === 'none') continue;

                const rect = el.getBoundingClientRect();
                if (rect.width < 10 || rect.height < 10) continue;
                if (rect.bottom < 0 || rect.right < 0 || rect.left > viewportWidth || rect.top > viewportHeight) {
                    continue;
                }

                const cx = rect.left + rect.width / 2;
                const cy = rect.top + rect.height / 2;
                if (!Number.isFinite(cx) || !Number.isFinite(cy)) continue;

                const topNode = document.elementFromPoint(cx, cy);
                if (topNode && topNode instanceof Element && !el.contains(topNode) && !topNode.contains(el)) {
                    continue;
                }

                const area = Math.min(rect.width * rect.height, 12000);
                const distFromCenter = Math.hypot(cx - viewportWidth / 2, cy - viewportHeight / 2);
                const centerScore = Math.max(0, 1 - distFromCenter / (diag * 0.55));
                const edgeDist = Math.min(cx, cy, viewportWidth - cx, viewportHeight - cy);
                const edgePenalty = edgeDist < 8 ? 40 : 0;
                const pointerBoost = style.cursor === 'pointer' ? 120 : 0;
                const tagBoost = ({ button: 170, a: 130, input: 110, select: 110, textarea: 100 })[el.tagName.toLowerCase()] || 80;

                const text = (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 40);
                const key = elementKey(el, cx, cy, text);
                const href = el instanceof HTMLAnchorElement ? (el.getAttribute('href') || '') : '';
                const dynamicBoost = /menu|nav|tab|card|tile|cta|action|play|pause|open|calc|form|wizard|step|option|choice|result/i.test((el.className || '').toString()) ? 30 : 0;

                out.push({
                    x: Math.max(2, Math.min(viewportWidth - 2, cx)),
                    y: Math.max(2, Math.min(viewportHeight - 2, cy)),
                    width: rect.width,
                    height: rect.height,
                    score: area * 0.02 + centerScore * 100 + pointerBoost + tagBoost + dynamicBoost - edgePenalty,
                    key,
                    text,
                    href,
                    tag: el.tagName.toLowerCase()
                });
            }

            out.sort((a, b) => b.score - a.score);
            return out.slice(0, Math.max(limit * 4, limit));
        }
        """,
        {
            "limit": limit,
            "viewportWidth": viewport_width,
            "viewportHeight": viewport_height,
        },
    )


def entry_click_score(target: Dict[str, Any], viewport_width: int, viewport_height: int) -> float:
    """Оценивает, насколько элемент похож на входную кнопку (Enter/Start/Continue)."""
    text = str(target.get("text", "")).strip().lower()
    href = str(target.get("href", "")).strip().lower()
    tag = str(target.get("tag", "")).strip().lower()

    keywords = (
        "enter",
        "start",
        "continue",
        "explore",
        "open",
        "go",
        "begin",
        "launch",
        "proceed",
        "visit",
        "view",
    )

    keyword_score = 0.0
    if any(word in text for word in keywords):
        keyword_score += 420.0
    if any(word in href for word in keywords):
        keyword_score += 140.0

    if tag in {"button", "a"}:
        keyword_score += 60.0

    x = float(target.get("x", 0.0))
    y = float(target.get("y", 0.0))
    width = float(target.get("width", 0.0))
    height = float(target.get("height", 0.0))
    area = width * height

    diag = math.hypot(viewport_width, viewport_height)
    center_dist = math.hypot(x - viewport_width / 2, y - viewport_height / 2)
    center_bonus = max(0.0, 1.0 - center_dist / max(diag * 0.45, 1.0)) * 180.0

    area_bonus = 0.0
    if 200 <= area <= 25000:
        area_bonus = 110.0

    return float(target.get("score", 0.0)) + keyword_score + center_bonus + area_bonus


async def collect_activation_targets(
    page: Any,
    viewport_width: int,
    viewport_height: int,
    limit: int,
) -> List[Dict[str, Any]]:
    """Собирает кандидатов для первичной активации страницы (enter/cookie/overlay)."""
    return await page.evaluate(
        """
        ({ limit, viewportWidth, viewportHeight }) => {
            const selectors = [
                'a[href]',
                'button',
                '[role="button"]',
                '[onclick]',
                '[tabindex]:not([tabindex="-1"])',
                '[aria-label]',
                '[class*="enter"]',
                '[class*="start"]',
                '[class*="continue"]',
                '[class*="accept"]',
                '[class*="cookie"]',
                '[id*="enter"]',
                '[id*="start"]'
            ];

            function toNumber(value, fallback) {
                const n = Number(value);
                return Number.isFinite(n) ? n : fallback;
            }

            function extractClickable(node) {
                if (!node || !(node instanceof Element)) return null;
                const clickable = node.closest('a,button,[role="button"],[onclick],[tabindex]:not([tabindex="-1"])');
                if (clickable && clickable instanceof HTMLElement) return clickable;

                if (node instanceof HTMLElement) {
                    const style = window.getComputedStyle(node);
                    const rect = node.getBoundingClientRect();
                    const isPointer = style.cursor === 'pointer';
                    const hasSize = rect.width >= 14 && rect.height >= 14;
                    if (isPointer && hasSize) return node;
                }

                return null;
            }

            const points = [
                [viewportWidth / 2, viewportHeight / 2],
                [viewportWidth / 2, viewportHeight * 0.58],
                [viewportWidth / 2, viewportHeight * 0.42],
                [viewportWidth * 0.35, viewportHeight / 2],
                [viewportWidth * 0.65, viewportHeight / 2],
            ];

            const pool = new Set();
            for (const node of document.querySelectorAll(selectors.join(','))) {
                if (node instanceof HTMLElement) pool.add(node);
            }

            for (const [x, y] of points) {
                const nodes = document.elementsFromPoint(x, y) || [];
                for (const node of nodes.slice(0, 8)) {
                    const clickable = extractClickable(node);
                    if (clickable) pool.add(clickable);
                }
            }

            const out = [];
            for (const el of pool) {
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || '1') < 0.05) {
                    continue;
                }

                const rect = el.getBoundingClientRect();
                if (rect.width < 8 || rect.height < 8) continue;
                if (rect.bottom < 0 || rect.right < 0 || rect.left > viewportWidth || rect.top > viewportHeight) continue;

                const cx = rect.left + rect.width / 2;
                const cy = rect.top + rect.height / 2;
                if (!Number.isFinite(cx) || !Number.isFinite(cy)) continue;

                const text = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim().slice(0, 80);
                const href = el instanceof HTMLAnchorElement ? (el.getAttribute('href') || '') : '';
                const z = toNumber(style.zIndex, 0);
                const area = rect.width * rect.height;
                const centerDist = Math.hypot(cx - viewportWidth / 2, cy - viewportHeight / 2);
                const centerScore = Math.max(0, 1 - centerDist / Math.hypot(viewportWidth / 2, viewportHeight / 2));

                const idPart = el.id ? `#${el.id}` : '';
                const classPart = (el.className || '').toString().replace(/\\s+/g, '.').slice(0, 40);
                const key = `${el.tagName}${idPart}.${classPart}|${Math.round(cx)}:${Math.round(cy)}|${text.slice(0, 30)}`;

                out.push({
                    key,
                    x: Math.max(2, Math.min(viewportWidth - 2, cx)),
                    y: Math.max(2, Math.min(viewportHeight - 2, cy)),
                    width: rect.width,
                    height: rect.height,
                    text,
                    href,
                    tag: el.tagName.toLowerCase(),
                    zIndex: z,
                    score: centerScore * 100 + Math.min(area, 40000) * 0.004 + (style.cursor === 'pointer' ? 60 : 0)
                });
            }

            out.sort((a, b) => b.score - a.score);
            return out.slice(0, Math.max(limit, 1));
        }
        """,
        {
            "limit": limit,
            "viewportWidth": viewport_width,
            "viewportHeight": viewport_height,
        },
    )


def activation_target_score(target: Dict[str, Any], viewport_width: int, viewport_height: int) -> float:
    """Универсальный скор для первичного клика: enter, cookie, continue, close, централизованные CTA."""
    text = str(target.get("text", "")).strip().lower()
    href = str(target.get("href", "")).strip().lower()
    tag = str(target.get("tag", "")).strip().lower()
    z_index = float(target.get("zIndex", 0.0))

    primary_keywords = (
        "enter", "start", "continue", "explore", "open", "go", "begin", "launch", "proceed",
        "visit", "view", "discover", "watch", "play", "skip", "next", "close", "ok", "accept",
        "agree", "allow", "got it", "i understand", "войти", "начать", "продолж", "далее", "принять",
    )

    keyword_bonus = 0.0
    if any(word in text for word in primary_keywords):
        keyword_bonus += 430.0
    if any(word in href for word in primary_keywords):
        keyword_bonus += 120.0

    if tag in {"button", "a"}:
        keyword_bonus += 55.0

    x = float(target.get("x", 0.0))
    y = float(target.get("y", 0.0))
    width = float(target.get("width", 0.0))
    height = float(target.get("height", 0.0))
    area = width * height

    diag = math.hypot(viewport_width, viewport_height)
    center_dist = math.hypot(x - viewport_width / 2, y - viewport_height / 2)
    center_bonus = max(0.0, 1.0 - center_dist / max(diag * 0.55, 1.0)) * 200.0

    area_bonus = 0.0
    if 120 <= area <= 90000:
        area_bonus = 100.0

    z_bonus = clamp(z_index, 0.0, 1000.0) * 0.08

    return float(target.get("score", 0.0)) + keyword_bonus + center_bonus + area_bonus + z_bonus


async def try_click_entry_element(
    page: Any,
    cursor_pos: Tuple[float, float],
    viewport_width: int,
    viewport_height: int,
    clicked_keys: Optional[Set[str]] = None,
) -> Tuple[Tuple[float, float], bool, Optional[str]]:
    """Пытается кликнуть по входному элементу, если страница заблокирована welcome/gate-экраном."""
    current_url = str(page.url or "")
    entry_words = (
        "enter", "start", "continue", "explore", "open", "go", "begin", "launch", "proceed",
        "skip", "next", "accept", "agree", "allow", "ok", "войти", "начать", "продолж", "далее", "принять",
    )
    purchase_words = (
        "buy", "shop", "cart", "checkout", "pricing", "price", "guide", "ebook", "course",
        "purchase", "subscribe", "plan", "membership", "donate", "book", "store",
    )

    try:
        targets = await collect_activation_targets(page, viewport_width, viewport_height, 36)
    except Exception as exc:
        if _is_nav_error(exc):
            await _recover_after_nav(page)
            return cursor_pos, False, None
        raise
    if not targets:
        return cursor_pos, False, None

    ranked = sorted(
        targets,
        key=lambda item: activation_target_score(item, viewport_width, viewport_height),
        reverse=True,
    )
    best: Optional[Dict[str, Any]] = None
    for candidate in ranked:
        candidate_key = str(candidate.get("key", ""))
        if clicked_keys is not None and candidate_key in clicked_keys:
            continue

        text = str(candidate.get("text", "")).strip().lower()
        href = str(candidate.get("href", "")).strip().lower()

        if has_keyword(text, purchase_words) or has_keyword(href, purchase_words):
            continue

        if href and is_navigation_like_href(href, current_url):
            # По требованию: не кликаем элементы, уводящие на другую страницу.
            continue

        # Без явных признаков входа не кликаем "случайные" ссылки.
        if href and not has_keyword(text, entry_words) and not has_keyword(href, entry_words):
            candidate_score = float(candidate.get("score", 0.0))
            width = float(candidate.get("width", 0.0))
            height = float(candidate.get("height", 0.0))
            x = float(candidate.get("x", 0.0))
            y = float(candidate.get("y", 0.0))
            center_dist = math.hypot(x - viewport_width / 2, y - viewport_height / 2)

            # Разрешаем клик по нестандартным контролам без текста,
            # если они визуально похожи на центральный activation-trigger.
            if not (
                candidate_score >= 85
                and width >= 18
                and height >= 18
                and center_dist <= math.hypot(viewport_width, viewport_height) * 0.35
            ):
                continue

        best = candidate
        break

    if best is None:
        return cursor_pos, False, None

    best_score = activation_target_score(best, viewport_width, viewport_height)

    if best_score < 360:
        return cursor_pos, False, None

    tx = float(best["x"])
    ty = float(best["y"])
    text_hint = str(best.get("text", "")).strip()

    logger.info(f"🖱️ Smart cursor: клик по входному элементу '{text_hint[:30]}'")

    cursor_pos = await move_mouse_human_like(
        page,
        cursor_pos,
        (tx, ty),
        viewport_width,
        viewport_height,
        random.randint(380, 980),
    )
    await page.wait_for_timeout(random.randint(90, 240))
    await page.mouse.click(tx, ty, delay=random.randint(40, 130))

    # Даем странице время снять оверлей и/или перейти дальше.
    await page.wait_for_timeout(random.randint(900, 1700))
    try:
        await page.wait_for_load_state("networkidle", timeout=3000)
    except Exception:
        pass

    return cursor_pos, True, str(best.get("key", ""))


async def get_page_activity_snapshot(page: Any) -> Dict[str, Any]:
    """Легкий снимок состояния страницы для оценки, произошли ли изменения после клика."""
    _default = {"url": str(page.url or ""), "scrollY": 0, "height": 0, "title": "", "text": ""}
    try:
        result = await page.evaluate(
            """
            () => {
                const text = (document.body?.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 300);
                return {
                    url: location.href,
                    scrollY: window.scrollY,
                    height: document.documentElement?.scrollHeight || 0,
                    title: document.title || '',
                    text
                };
            }
            """
        )
        return result if isinstance(result, dict) else _default
    except Exception as exc:
        if _is_nav_error(exc):
            await _recover_after_nav(page)
            return _default
        raise


async def get_scroll_metrics(page: Any) -> Dict[str, Any]:
    _default = {"scrollY": 0, "maxScroll": 0, "atBottom": False}
    try:
        result = await page.evaluate(
            """
            () => {
                const doc = document.documentElement;
                const scrollY = Math.round(window.scrollY || 0);
                const maxScroll = Math.max(0, Math.round((doc?.scrollHeight || 0) - (window.innerHeight || 0)));
                return {
                    scrollY,
                    maxScroll,
                    atBottom: scrollY >= (maxScroll - 3),
                };
            }
            """
        )
        return result if isinstance(result, dict) else _default
    except Exception as exc:
        if _is_nav_error(exc):
            await _recover_after_nav(page)
            return _default
        raise


def page_state_changed(before: Dict[str, Any], after: Dict[str, Any]) -> bool:
    return (
        before.get("url") != after.get("url")
        or int(after.get("scrollY", 0)) != int(before.get("scrollY", 0))
        or str(before.get("text", "")) != str(after.get("text", ""))
        or str(before.get("title", "")) != str(after.get("title", ""))
    )


def has_keyword(value: str, keywords: Tuple[str, ...]) -> bool:
    low = value.lower()
    return any(word in low for word in keywords)


def is_navigation_like_href(href: str, current_url: str) -> bool:
    clean = href.strip().lower()
    if not clean or clean.startswith("#") or clean.startswith("javascript:"):
        return False

    resolved = urljoin(current_url, href)
    current_parts = urlparse(current_url)
    resolved_parts = urlparse(resolved)

    if resolved_parts.scheme not in {"http", "https"}:
        return False

    if resolved_parts.netloc != current_parts.netloc:
        return True

    # Если путь/параметры отличаются - это переход на другую страницу.
    return (resolved_parts.path != current_parts.path) or (resolved_parts.query != current_parts.query)


def is_external_href(href: str, current_url: str) -> bool:
    clean = href.strip().lower()
    if not clean or clean.startswith("#") or clean.startswith("javascript:"):
        return False

    resolved = urljoin(current_url, href)
    current_parts = urlparse(current_url)
    resolved_parts = urlparse(resolved)

    if resolved_parts.scheme not in {"http", "https"}:
        return False

    return resolved_parts.netloc != current_parts.netloc


def is_nav_tab_self_link(target: Dict[str, Any], current_url: str) -> bool:
    """Ссылка указывает на текущую страницу — кликать бесполезно и может вызвать перезагрузку."""
    href = str(target.get("href", "")).strip()
    if not href or href.startswith("#") or href.startswith("javascript:"):
        return False
    resolved = urljoin(current_url, href)
    current_parts = urlparse(current_url)
    resolved_parts = urlparse(resolved)
    if resolved_parts.scheme not in ("http", "https"):
        return False
    if resolved_parts.netloc != current_parts.netloc:
        return False
    current_path = current_parts.path.rstrip("/")
    resolved_path = resolved_parts.path.rstrip("/")
    return resolved_path == current_path and (resolved_parts.query or "") == (current_parts.query or "")


def _is_nav_error(exc: Exception) -> bool:
    """Check if exception was caused by page navigation destroying the JS execution context."""
    msg = str(exc).lower()
    return any(s in msg for s in (
        "execution context was destroyed",
        "navigat",
        "target closed",
        "frame was detached",
        "frame has been detached",
        "context was destroyed",
    ))


async def _recover_after_nav(page, timeout: int = 15000) -> None:
    """Wait for the page to stabilize after an unexpected navigation."""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=min(timeout, 8000))
    except Exception:
        pass
    # Small grace period for JS frameworks to hydrate.
    try:
        await page.wait_for_timeout(500)
    except Exception:
        pass


def is_safe_inpage_click_target(target: Dict[str, Any], current_url: str, allow_internal_nav_click: bool) -> bool:
    """Разрешаем клик только по элементам, которые не уводят на другую страницу."""
    text = str(target.get("text", "")).strip().lower()
    href = str(target.get("href", "")).strip().lower()
    tag = str(target.get("tag", "")).strip().lower()

    if tag in {"input", "textarea", "select"}:
        return False

    purchase_words = (
        "buy", "shop", "cart", "checkout", "pricing", "price", "guide", "ebook", "course",
        "purchase", "subscribe", "plan", "membership", "donate", "book", "store", "order",
    )
    if has_keyword(text, purchase_words) or has_keyword(href, purchase_words):
        return False

    if href and is_external_href(href, current_url):
        return False

    if href and is_navigation_like_href(href, current_url) and not allow_internal_nav_click:
        return False

    width = float(target.get("width", 0.0))
    height = float(target.get("height", 0.0))
    score = float(target.get("score", 0.0))
    if width < 14 or height < 14:
        return False

    # Небольшой порог, чтобы случайные декоративные элементы не кликались.
    return score >= 55


def is_probable_top_nav_target(target: Dict[str, Any], viewport_height: int) -> bool:
    """Универсально определяет элементы верхней навигации (вкладки/пункты меню)."""
    y = float(target.get("y", viewport_height))
    width = float(target.get("width", 0.0))
    height = float(target.get("height", 0.0))
    text = str(target.get("text", "")).strip().lower()
    href = str(target.get("href", "")).strip().lower()
    tag = str(target.get("tag", "")).strip().lower()

    if y > viewport_height * 0.24:
        return False

    if width < 28 or width > 420 or height < 10 or height > 120:
        return False

    if tag not in {"a", "button", "div", "span"}:
        return False

    if not text and not href:
        return False

    # Фильтруем системные и сервисные элементы шапки.
    blocked_words = (
        "login", "sign in", "account", "cookie", "privacy", "terms", "cart", "shop", "buy", "subscribe",
    )
    if has_keyword(text, blocked_words) or has_keyword(href, blocked_words):
        return False

    # Типичная длина текста вкладок/разделов.
    if text and len(text) > 28:
        return False

    return True


def nav_signature(target: Dict[str, Any]) -> str:
    text = str(target.get("text", "")).strip().lower()
    href = str(target.get("href", "")).strip().lower()
    x = int(float(target.get("x", 0.0)))
    y = int(float(target.get("y", 0.0)))
    return f"{text}|{href}|{x}:{y}"


def target_sort_score(target: Dict[str, Any]) -> float:
    return float(target.get("score", 0.0))


def nav_family_key(target: Dict[str, Any]) -> str:
    """Грубый ключ для дедупликации одинаковых пунктов в шапке/меню."""
    text = str(target.get("text", "")).strip().lower()
    href = str(target.get("href", "")).strip().lower()
    tag = str(target.get("tag", "")).strip().lower()
    x = int(float(target.get("x", 0.0)))

    text_token = text[:24]
    href_token = href.split("?")[0][:36]
    x_bucket = int(x / 120)
    return f"{tag}|{text_token}|{href_token}|{x_bucket}"


async def collect_close_targets(page: Any, viewport_width: int, viewport_height: int) -> List[Dict[str, Any]]:
    """Ищет кнопки закрытия модалок/lightbox (Close/X/Dismiss/Cancel)."""
    return await page.evaluate(
        """
        ({ viewportWidth, viewportHeight }) => {
            const selectors = [
                'button',
                '[role="button"]',
                '[aria-label]',
                '[title]',
                '[class*="close"]',
                '[class*="dismiss"]',
                '[data-close]',
                '[data-dismiss]',
                '[aria-modal="true"] button',
                '[role="dialog"] button'
            ];

            const closeWords = ['close', 'dismiss', 'cancel', 'exit', 'back', 'done', 'x', 'закрыть', 'крестик'];
            const out = [];
            const pool = new Set(document.querySelectorAll(selectors.join(',')));

            for (const el of pool) {
                if (!(el instanceof HTMLElement)) continue;

                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || '1') < 0.05) {
                    continue;
                }

                const rect = el.getBoundingClientRect();
                if (rect.width < 8 || rect.height < 8) continue;
                if (rect.bottom < 0 || rect.right < 0 || rect.left > viewportWidth || rect.top > viewportHeight) continue;

                const text = (el.innerText || '').trim().toLowerCase();
                const aria = (el.getAttribute('aria-label') || '').trim().toLowerCase();
                const title = (el.getAttribute('title') || '').trim().toLowerCase();
                const cls = (el.className || '').toString().toLowerCase();

                const isCloseLike = closeWords.some(w => text.includes(w) || aria.includes(w) || title.includes(w) || cls.includes(w));
                if (!isCloseLike) continue;

                const cx = rect.left + rect.width / 2;
                const cy = rect.top + rect.height / 2;
                if (!Number.isFinite(cx) || !Number.isFinite(cy)) continue;

                const nearTopRight = (cx > viewportWidth * 0.6 && cy < viewportHeight * 0.32) ? 120 : 0;
                const score = nearTopRight + (style.cursor === 'pointer' ? 70 : 0) + Math.min(rect.width * rect.height, 4000) * 0.02;

                out.push({
                    x: Math.max(2, Math.min(viewportWidth - 2, cx)),
                    y: Math.max(2, Math.min(viewportHeight - 2, cy)),
                    score,
                    text: (text || aria || title).slice(0, 40),
                    key: `${Math.round(cx)}:${Math.round(cy)}|${(text || aria || title).slice(0, 20)}`,
                });
            }

            out.sort((a, b) => b.score - a.score);
            return out.slice(0, 6);
        }
        """,
        {
            "viewportWidth": viewport_width,
            "viewportHeight": viewport_height,
        },
    )


async def try_close_overlay(
    page: Any,
    cursor_pos: Tuple[float, float],
    viewport_width: int,
    viewport_height: int,
) -> Tuple[Tuple[float, float], bool]:
    """Пытается закрыть открытые модалки/lightbox, чтобы курсор не застревал в галерее."""
    try:
        before = await get_page_activity_snapshot(page)
    except Exception as exc:
        if _is_nav_error(exc):
            await _recover_after_nav(page)
            return cursor_pos, False
        raise

    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(random.randint(60, 140))
    except Exception:
        pass

    try:
        close_targets = await collect_close_targets(page, viewport_width, viewport_height)
    except Exception as exc:
        if _is_nav_error(exc):
            await _recover_after_nav(page)
            return cursor_pos, False
        raise

    for item in close_targets[:2]:
        tx = float(item.get("x", viewport_width * 0.92))
        ty = float(item.get("y", viewport_height * 0.08))

        try:
            cursor_pos = await move_mouse_human_like(
                page,
                cursor_pos,
                (tx, ty),
                viewport_width,
                viewport_height,
                random.randint(220, 540),
            )
            await page.wait_for_timeout(random.randint(50, 120))
            await page.mouse.click(tx, ty, delay=random.randint(28, 90))
            await page.wait_for_timeout(random.randint(160, 340))
        except Exception as exc:
            if _is_nav_error(exc):
                await _recover_after_nav(page)
                return cursor_pos, True
            raise

        try:
            after = await get_page_activity_snapshot(page)
            if page_state_changed(before, after):
                logger.info("🖱️ Smart cursor: закрыта модалка/галерея")
                return cursor_pos, True
        except Exception as exc:
            if _is_nav_error(exc):
                await _recover_after_nav(page)
                return cursor_pos, True
            raise

    return cursor_pos, False


async def perform_smooth_scroll(
    page: Any,
    viewport_height: int,
    scroll_speed_factor: float,
    scroll_pause_min_ms: int,
    scroll_pause_max_ms: int,
) -> None:
    """Плавный скролл небольшими шагами вместо одного резкого прыжка."""
    total_delta = int(viewport_height * random.uniform(0.26, 0.46) * scroll_speed_factor)
    steps = int(clamp(random.randint(4, 8), 3, 10))
    base_step = max(20, int(total_delta / max(steps, 1)))

    for _ in range(steps):
        step_delta = max(12, int(base_step + random.uniform(-18, 22)))
        await page.mouse.wheel(0, step_delta)
        try:
            await page.evaluate(
                """(delta) => window.scrollBy({ top: delta, left: 0, behavior: 'auto' })""",
                step_delta,
            )
        except Exception:
            pass
        await page.wait_for_timeout(random.randint(scroll_pause_min_ms, scroll_pause_max_ms))


async def force_scroll_to_page_end(
    page: Any,
    viewport_height: int,
    scroll_speed_factor: float,
    scroll_pause_min_ms: int,
    scroll_pause_max_ms: int,
    finish_timeout_ms: int,
) -> bool:
    """Финальный проход: агрессивно доскролливает страницу до конца, если основной цикл не успел."""
    if finish_timeout_ms <= 0:
        return False

    start = time.monotonic()
    stable_rounds = 0
    last_scroll = -1

    while (time.monotonic() - start) * 1000 < finish_timeout_ms:
        await perform_smooth_scroll(
            page,
            viewport_height,
            max(scroll_speed_factor, 1.0),
            max(18, int(scroll_pause_min_ms * 0.7)),
            max(28, int(scroll_pause_max_ms * 0.8)),
        )

        try:
            metrics = await get_scroll_metrics(page)
            current_scroll = int(metrics.get("scrollY", last_scroll))
            at_bottom = bool(metrics.get("atBottom", False))
        except Exception:
            current_scroll = last_scroll
            at_bottom = False

        if current_scroll <= last_scroll + 2:
            try:
                await page.keyboard.press("PageDown")
                await page.wait_for_timeout(random.randint(26, 70))
            except Exception:
                pass

            try:
                await page.keyboard.press("End")
                await page.wait_for_timeout(random.randint(60, 120))
            except Exception:
                pass

        if at_bottom:
            stable_rounds += 1
        else:
            stable_rounds = 0

        last_scroll = max(last_scroll, current_scroll)
        if stable_rounds >= 2:
            return True

    return False


async def collect_header_nav_targets(
    page: Any,
    viewport_width: int,
    viewport_height: int,
    allow_internal_nav_click: bool,
    visited_nav_keys: Set[str],
) -> List[Dict[str, Any]]:
    """Целенаправленно собирает пункты верхнего меню/вкладок из header/nav-структур."""
    try:
        raw = await page.evaluate(
        """
        ({ viewportWidth, viewportHeight }) => {
            function isVisible(el) {
                if (!(el instanceof HTMLElement)) return false;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                if (Number(style.opacity || '1') < 0.02) return false;
                if (style.pointerEvents === 'none') return false;
                const rect = el.getBoundingClientRect();
                return rect.width >= 18 && rect.height >= 10;
            }

            function clickableFrom(node) {
                if (!node || !(node instanceof Element)) return null;
                const clickable = node.closest('a,button,[role="button"],[role="link"],[role="menuitem"],[onclick],[tabindex]:not([tabindex="-1"])');
                return clickable instanceof HTMLElement ? clickable : null;
            }

            const navRoots = [
                ...document.querySelectorAll('header nav, header, nav, [role="navigation"], [aria-label*="nav" i], [class*="nav" i], [class*="menu" i], [id*="nav" i], [id*="menu" i]')
            ];

            const pool = new Set();
            for (const root of navRoots) {
                if (!(root instanceof HTMLElement)) continue;
                const rr = root.getBoundingClientRect();
                if (rr.top > viewportHeight * 0.35 || rr.bottom < 0) continue;

                for (const node of root.querySelectorAll('a,button,[role="button"],[role="link"],[role="menuitem"],[onclick],[tabindex]:not([tabindex="-1"])')) {
                    if (node instanceof HTMLElement) pool.add(node);
                }
            }

            // Страховка для сайтов, где nav нет в семантике: собираем кликабельные элементы по точкам в верхней зоне.
            const scanRows = [0.055, 0.085, 0.12, 0.16, 0.20];
            const cols = 12;
            for (const row of scanRows) {
                const y = viewportHeight * row;
                for (let c = 0; c < cols; c++) {
                    const x = ((c + 0.5) / cols) * viewportWidth;
                    const stack = document.elementsFromPoint(x, y) || [];
                    for (const node of stack.slice(0, 6)) {
                        const clickable = clickableFrom(node);
                        if (clickable) pool.add(clickable);
                    }
                }
            }

            const out = [];
            for (const el of pool) {
                if (!isVisible(el)) continue;
                const rect = el.getBoundingClientRect();
                const cx = rect.left + rect.width / 2;
                const cy = rect.top + rect.height / 2;
                if (!Number.isFinite(cx) || !Number.isFinite(cy)) continue;
                if (cy > viewportHeight * 0.34 || cx < 0 || cx > viewportWidth) continue;

                const text = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim().replace(/\\s+/g, ' ').slice(0, 64);
                const href = el instanceof HTMLAnchorElement ? (el.getAttribute('href') || '') : '';
                const tag = el.tagName.toLowerCase();
                const cls = (el.className || '').toString().toLowerCase();

                const score =
                    (cy <= viewportHeight * 0.18 ? 120 : 70)
                    + Math.max(0, 90 - Math.abs(cy - viewportHeight * 0.11) * 1.2)
                    + (tag === 'a' || tag === 'button' ? 45 : 18)
                    + (text.length > 0 ? 30 : 0)
                    + (/nav|menu|tab|item/.test(cls) ? 28 : 0)
                    + Math.min(rect.width * rect.height, 5000) * 0.006;

                out.push({
                    x: Math.max(2, Math.min(viewportWidth - 2, cx)),
                    y: Math.max(2, Math.min(viewportHeight - 2, cy)),
                    width: rect.width,
                    height: rect.height,
                    score,
                    text,
                    href,
                    tag,
                    key: `${tag}|${text.slice(0, 28)}|${href.slice(0, 36)}|${Math.round(cx)}:${Math.round(cy)}`,
                });
            }

            return out;
        }
        """,
        {
            "viewportWidth": viewport_width,
            "viewportHeight": viewport_height,
        },
    )
    except Exception as exc:
        if _is_nav_error(exc):
            await _recover_after_nav(page)
            return []
        raise

    current_url = str(page.url or "")
    safe = [
        item for item in (raw or [])
        if nav_signature(item) not in visited_nav_keys
        and is_probable_top_nav_target(item, viewport_height)
        and is_safe_inpage_click_target(item, current_url, allow_internal_nav_click)
        and not is_nav_tab_self_link(item, current_url)
    ]

    unique: Dict[str, Dict[str, Any]] = {}
    for item in safe:
        family = nav_family_key(item)
        prev = unique.get(family)
        if prev is None or target_sort_score(item) > target_sort_score(prev):
            unique[family] = item

    ordered = list(unique.values())
    ordered.sort(key=lambda item: (float(item.get("x", 0.0)), float(item.get("y", 0.0)), -target_sort_score(item)))
    return ordered[:14]


async def collect_top_nav_targets(
    page: Any,
    viewport_width: int,
    viewport_height: int,
    allow_internal_nav_click: bool,
    visited_nav_keys: Set[str],
) -> List[Dict[str, Any]]:
    """Собирает кандидатов верхней навигации и сортирует слева направо."""
    try:
        header_nav = await collect_header_nav_targets(
            page,
            viewport_width,
            viewport_height,
            allow_internal_nav_click,
            visited_nav_keys,
        )
    except Exception as exc:
        if _is_nav_error(exc):
            await _recover_after_nav(page)
            return []
        raise
    if header_nav:
        return header_nav

    try:
        targets = await collect_interactive_targets(page, viewport_width, viewport_height, 80)
    except Exception as exc:
        if _is_nav_error(exc):
            await _recover_after_nav(page)
            return []
        raise
    current_url = str(page.url or "")

    candidates = [
        item for item in targets
        if is_probable_top_nav_target(item, viewport_height)
        and nav_signature(item) not in visited_nav_keys
        and is_safe_inpage_click_target(item, current_url, allow_internal_nav_click)
        and not is_nav_tab_self_link(item, current_url)
    ]

    if not candidates:
        return []

    # Оставляем наиболее вероятный "ряд" навигации в верхней части страницы.
    row_anchor_y = min(float(item.get("y", viewport_height * 0.2)) for item in candidates)
    row_candidates = [
        item for item in candidates
        if float(item.get("y", viewport_height * 0.2)) <= row_anchor_y + 68
    ]

    unique: Dict[str, Dict[str, Any]] = {}
    for item in row_candidates:
        family = nav_family_key(item)
        prev = unique.get(family)
        if prev is None or target_sort_score(item) > target_sort_score(prev):
            unique[family] = item

    ordered = list(unique.values())
    ordered.sort(key=lambda item: (float(item.get("x", 0.0)), float(item.get("y", 0.0)), -target_sort_score(item)))
    return ordered[:14]


async def visit_top_navigation_tabs(
    page: Any,
    cursor_pos: Tuple[float, float],
    viewport_width: int,
    viewport_height: int,
    allow_internal_nav_click: bool,
    visited_nav_keys: Set[str],
    max_nav_tabs_to_visit: int,
    per_tab_scroll_timeout_ms: int,
    scroll_speed_factor: float,
    scroll_pause_min_ms: int,
    scroll_pause_max_ms: int,
) -> Tuple[Tuple[float, float], int]:
    """Проходит по вкладкам верхней навигации последовательно, а не случайно."""
    visited_count = 0
    if max_nav_tabs_to_visit <= 0:
        return cursor_pos, visited_count

    original_url = str(page.url or "")

    for tab_iter in range(max_nav_tabs_to_visit):
        # ── Вся итерация обёрнута для отказоустойчивости ──
        try:
            nav_targets = await collect_top_nav_targets(
                page,
                viewport_width,
                viewport_height,
                allow_internal_nav_click,
                visited_nav_keys,
            )
        except Exception as exc:
            if _is_nav_error(exc):
                logger.warning("⚠️ Smart cursor: навигация при сборе вкладок, восстанавливаемся")
                await _recover_after_nav(page)
                continue
            raise

        if not nav_targets:
            break

        target = nav_targets[0]

        # Дополнительная проверка: self-link мог просочиться из-за изменения URL
        current_url = str(page.url or "")
        if is_nav_tab_self_link(target, current_url):
            visited_nav_keys.add(nav_signature(target))
            continue

        try:
            before_tab = await get_page_activity_snapshot(page)
        except Exception as exc:
            if _is_nav_error(exc):
                await _recover_after_nav(page)
                before_tab = {"url": current_url, "scrollY": 0, "height": 0, "title": "", "text": ""}
            else:
                raise

        tx = float(target.get("x", viewport_width * 0.5))
        ty = float(target.get("y", viewport_height * 0.12))

        try:
            cursor_pos = await move_mouse_human_like(
                page,
                cursor_pos,
                (tx, ty),
                viewport_width,
                viewport_height,
                random.randint(280, 760),
            )
            await page.wait_for_timeout(random.randint(60, 180))
        except Exception as exc:
            if _is_nav_error(exc):
                await _recover_after_nav(page)
                visited_nav_keys.add(nav_signature(target))
                continue
            raise

        # Запоминаем URL до клика для обнаружения навигации.
        url_before_click = str(page.url or "")

        try:
            await page.mouse.click(tx, ty, delay=random.randint(28, 90))
            await page.wait_for_timeout(random.randint(350, 900))
        except Exception as exc:
            if _is_nav_error(exc):
                await _recover_after_nav(page)
                visited_nav_keys.add(nav_signature(target))
                visited_count += 1
                logger.info(f"🧭 Smart cursor: вкладка вызвала навигацию '{str(target.get('text', '')).strip()[:32]}'")
                # Скроллим новую страницу и возвращаемся.
                await _scroll_and_return(
                    page, original_url, viewport_height, scroll_speed_factor,
                    scroll_pause_min_ms, scroll_pause_max_ms, per_tab_scroll_timeout_ms
                )
                continue
            raise

        # Проверяем, изменился ли URL (быстрая проверка без evaluate).
        url_after_click = str(page.url or "")
        navigated_away = url_after_click != url_before_click

        # Если URL изменился — ждём загрузки новой страницы.
        if navigated_away:
            await _recover_after_nav(page)

        # Проверяем состояние страницы.
        try:
            after_tab = await get_page_activity_snapshot(page)
            changed = page_state_changed(before_tab, after_tab) or navigated_away
        except Exception:
            changed = True
            await _recover_after_nav(page)

        if not changed:
            # Повторная попытка с легким смещением на случай мелких hitbox/overlay.
            retry_x = clamp(tx + random.uniform(-8, 8), 1, viewport_width - 1)
            retry_y = clamp(ty + random.uniform(-6, 6), 1, viewport_height - 1)
            try:
                await page.mouse.click(retry_x, retry_y, delay=random.randint(24, 80))
                await page.wait_for_timeout(random.randint(260, 640))
            except Exception:
                pass

            url_after_retry = str(page.url or "")
            if url_after_retry != url_before_click:
                navigated_away = True
                await _recover_after_nav(page)

            try:
                after_retry = await get_page_activity_snapshot(page)
                changed = page_state_changed(before_tab, after_retry) or navigated_away
            except Exception:
                changed = True
                await _recover_after_nav(page)

        signature = nav_signature(target)
        visited_nav_keys.add(signature)
        if changed:
            visited_count += 1
            logger.info(f"🧭 Smart cursor: открыта вкладка '{str(target.get('text', '')).strip()[:32]}'")
        else:
            logger.info(f"🧭 Smart cursor: пропуск неактивной вкладки '{str(target.get('text', '')).strip()[:32]}'")

        # Закрытие модалок (полностью безопасно — try_close_overlay уже nav-safe).
        try:
            cursor_pos, _ = await try_close_overlay(page, cursor_pos, viewport_width, viewport_height)
        except Exception:
            await _recover_after_nav(page)

        if changed:
            try:
                await force_scroll_to_page_end(
                    page,
                    viewport_height,
                    scroll_speed_factor,
                    scroll_pause_min_ms,
                    scroll_pause_max_ms,
                    per_tab_scroll_timeout_ms,
                )
            except Exception as exc:
                if _is_nav_error(exc):
                    await _recover_after_nav(page)
                else:
                    logger.warning(f"⚠️ Smart cursor: ошибка при скролле вкладки: {exc}")

        # Если навигация увела на другую страницу, возвращаемся на оригинальную.
        current_after = str(page.url or "")
        if navigated_away and current_after != original_url:
            try:
                await page.goto(original_url, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(random.randint(400, 900))
            except Exception:
                try:
                    await page.go_back(wait_until="domcontentloaded", timeout=10000)
                    await page.wait_for_timeout(random.randint(300, 700))
                except Exception:
                    pass

        # Возвращаемся к верху, чтобы снова видеть вкладки.
        try:
            await page.evaluate("""() => window.scrollTo({ top: 0, left: 0, behavior: 'auto' })""")
            await page.wait_for_timeout(random.randint(180, 420))
        except Exception:
            pass

    return cursor_pos, visited_count


async def _scroll_and_return(
    page: Any,
    original_url: str,
    viewport_height: int,
    scroll_speed_factor: float,
    scroll_pause_min_ms: int,
    scroll_pause_max_ms: int,
    scroll_timeout_ms: int,
) -> None:
    """Прокручивает текущую страницу до конца и возвращается на original_url."""
    try:
        await force_scroll_to_page_end(
            page, viewport_height, scroll_speed_factor,
            scroll_pause_min_ms, scroll_pause_max_ms, scroll_timeout_ms,
        )
    except Exception:
        pass

    current = str(page.url or "")
    if current != original_url:
        try:
            await page.goto(original_url, wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(random.randint(400, 900))
        except Exception:
            try:
                await page.go_back(wait_until="domcontentloaded", timeout=10000)
                await page.wait_for_timeout(random.randint(300, 700))
            except Exception:
                pass


async def perform_forced_activation_clicks(
    page: Any,
    cursor_pos: Tuple[float, float],
    viewport_width: int,
    viewport_height: int,
) -> Tuple[Tuple[float, float], bool]:
    """Fallback-активация: кликает по ключевым зонам и по top-element через elementFromPoint."""
    points: List[Tuple[float, float]] = [
        (viewport_width * 0.50, viewport_height * 0.52),
        (viewport_width * 0.50, viewport_height * 0.62),
        (viewport_width * 0.50, viewport_height * 0.42),
        (viewport_width * 0.38, viewport_height * 0.52),
        (viewport_width * 0.62, viewport_height * 0.52),
    ]

    snapshot_before = await get_page_activity_snapshot(page)

    for x, y in points:
        cx = clamp(x + random.uniform(-16, 16), 1, viewport_width - 1)
        cy = clamp(y + random.uniform(-12, 12), 1, viewport_height - 1)

        cursor_pos = await move_mouse_human_like(
            page,
            cursor_pos,
            (cx, cy),
            viewport_width,
            viewport_height,
            random.randint(260, 720),
        )
        await page.wait_for_timeout(random.randint(80, 180))

        await page.mouse.click(cx, cy, delay=random.randint(30, 120))

        # Дополнительно инициируем click по верхнему DOM-элементу в точке.
        try:
            await page.evaluate(
                """
                ({ x, y }) => {
                    const node = document.elementFromPoint(x, y);
                    if (!node || !(node instanceof Element)) return false;
                    const clickable = node.closest('a,button,[role="button"],[onclick],[tabindex]:not([tabindex="-1"])') || node;
                    if (!(clickable instanceof HTMLElement)) return false;
                    clickable.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                    return true;
                }
                """,
                {"x": cx, "y": cy},
            )
        except Exception:
            pass

        await page.wait_for_timeout(random.randint(500, 1200))
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=1200)
        except Exception:
            pass

        try:
            snapshot_after = await get_page_activity_snapshot(page)
            if page_state_changed(snapshot_before, snapshot_after):
                logger.info("🖱️ Smart cursor: fallback-клик изменил состояние страницы")
                return cursor_pos, True
        except Exception as exc:
            if _is_nav_error(exc):
                await _recover_after_nav(page)
                return cursor_pos, True
            raise

    return cursor_pos, False


async def move_mouse_human_like(
    page: Any,
    start: Tuple[float, float],
    end: Tuple[float, float],
    viewport_width: int,
    viewport_height: int,
    duration_ms: int,
) -> Tuple[float, float]:
    """Имитирует естественный плавный путь курсора с легкой нестабильностью."""
    start_x, start_y = start
    end_x, end_y = end

    distance = math.hypot(end_x - start_x, end_y - start_y)
    # Меньше шагов = меньше нагрузки на тяжелых WebGL-сайтах.
    base_steps = int(clamp(distance / 26, 8, 36))
    steps = int(clamp(base_steps + random.randint(-1, 4), 8, 42))

    dx = end_x - start_x
    dy = end_y - start_y
    length = max(distance, 1.0)
    ux = dx / length
    uy = dy / length
    # Перпендикуляр к направлению движения для мягкого изгиба.
    px = -uy
    py = ux

    lateral_amp = clamp(distance * 0.16, 8.0, 58.0)
    forward_jitter = clamp(distance * 0.10, 6.0, 42.0)

    cp1 = (
        start_x + dx * random.uniform(0.24, 0.42) + px * random.uniform(-lateral_amp, lateral_amp) + ux * random.uniform(-forward_jitter, forward_jitter),
        start_y + dy * random.uniform(0.24, 0.42) + py * random.uniform(-lateral_amp, lateral_amp) + uy * random.uniform(-forward_jitter, forward_jitter),
    )
    cp2 = (
        start_x + dx * random.uniform(0.58, 0.86) + px * random.uniform(-lateral_amp, lateral_amp) + ux * random.uniform(-forward_jitter, forward_jitter),
        start_y + dy * random.uniform(0.58, 0.86) + py * random.uniform(-lateral_amp, lateral_amp) + uy * random.uniform(-forward_jitter, forward_jitter),
    )

    for i in range(1, steps + 1):
        t = i / steps
        eased_t = t * t * (3 - 2 * t)

        x = cubic_bezier(eased_t, start_x, cp1[0], cp2[0], end_x)
        y = cubic_bezier(eased_t, start_y, cp1[1], cp2[1], end_y)

        # Шум уменьшается ближе к целевой точке, чтобы курсор "попадал" точно.
        noise_scale = (1 - eased_t) * 1.5
        x += random.uniform(-noise_scale, noise_scale)
        y += random.uniform(-noise_scale, noise_scale)

        x = clamp(x, 1, viewport_width - 1)
        y = clamp(y, 1, viewport_height - 1)

        await page.mouse.move(x, y)
        per_step_delay = max(6, int(duration_ms / steps + random.uniform(-2, 6)))
        await page.wait_for_timeout(per_step_delay)

    return end_x, end_y


async def run_smart_cursor(
    page: Any,
    viewport_width: int,
    viewport_height: int,
    total_time_ms: int,
    max_targets: int,
    hover_min_ms: int,
    hover_max_ms: int,
    entry_click_enabled: bool,
    entry_click_attempts: int,
    scroll_to_end: bool,
    bottom_stable_rounds_required: int,
    scroll_speed_factor: float,
    scroll_pause_min_ms: int,
    scroll_pause_max_ms: int,
    scroll_finish_timeout_ms: int,
    nav_tabs_visit_enabled: bool,
    nav_tabs_max_visits: int,
    nav_tab_scroll_timeout_ms: int,
    inpage_click_enabled: bool,
    inpage_click_probability: float,
    allow_internal_nav_click: bool,
) -> int:
    """Скроллит страницу до конца, параллельно ховерит и кликает безопасные in-page интерактивы."""
    start_time = time.monotonic()
    visited_keys: Set[str] = set()
    clicked_entry_keys: Set[str] = set()
    clicked_inpage_keys: Set[str] = set()
    hovered_count = 0
    round_index = 0
    bottom_stable_rounds = 0
    last_scroll_y = -1
    stagnant_scroll_rounds = 0
    recent_points: List[Tuple[float, float]] = []
    clicked_nav_keys: Set[str] = set()

    cursor_pos: Tuple[float, float] = (
        viewport_width * random.uniform(0.35, 0.65),
        viewport_height * random.uniform(0.35, 0.65),
    )
    await page.mouse.move(cursor_pos[0], cursor_pos[1])

    if entry_click_enabled:
        for _ in range(max(entry_click_attempts, 0)):
            try:
                cursor_pos, clicked, clicked_key = await try_click_entry_element(
                    page=page,
                    cursor_pos=cursor_pos,
                    viewport_width=viewport_width,
                    viewport_height=viewport_height,
                    clicked_keys=clicked_entry_keys,
                )
            except Exception as exc:
                if _is_nav_error(exc):
                    await _recover_after_nav(page)
                    break
                raise
            if not clicked:
                break
            if clicked_key:
                clicked_entry_keys.add(clicked_key)

    logger.info("🧭 Smart cursor: старт обхода интерактивных элементов")

    # Приоритет 1: гарантированно пройти главную страницу до конца.
    if scroll_to_end:
        main_scroll_timeout = max(6000, min(scroll_finish_timeout_ms, int(total_time_ms * 0.32)))
        try:
            reached_main_end = await force_scroll_to_page_end(
                page,
                viewport_height,
                scroll_speed_factor,
                scroll_pause_min_ms,
                scroll_pause_max_ms,
                main_scroll_timeout,
            )
            if reached_main_end:
                logger.info("🧭 Smart cursor: главная страница прокручена до конца")
        except Exception as exc:
            if _is_nav_error(exc):
                logger.warning("⚠️ Smart cursor: навигация при начальном скролле, восстанавливаемся")
                await _recover_after_nav(page)
            else:
                logger.warning(f"⚠️ Smart cursor: ошибка при начальном скролле: {exc}")

        try:
            await page.evaluate("""() => window.scrollTo({ top: 0, left: 0, behavior: 'auto' })""")
            await page.wait_for_timeout(random.randint(180, 420))
        except Exception:
            pass

    # Приоритет 2: пройти верхние вкладки последовательно (универсально, если они есть).
    if nav_tabs_visit_enabled and inpage_click_enabled:
        try:
            cursor_pos, visited_nav_count = await visit_top_navigation_tabs(
                page,
                cursor_pos,
                viewport_width,
                viewport_height,
                allow_internal_nav_click,
                clicked_nav_keys,
                nav_tabs_max_visits,
                nav_tab_scroll_timeout_ms,
                scroll_speed_factor,
                scroll_pause_min_ms,
                scroll_pause_max_ms,
            )
            if visited_nav_count > 0:
                logger.info(f"🧭 Smart cursor: последовательно пройдено вкладок {visited_nav_count}")
        except Exception as nav_err:
            logger.warning(f"⚠️ Smart cursor: ошибка при обходе вкладок, продолжаем: {nav_err}")
            await _recover_after_nav(page)

    try:
        last_scroll_y = int((await get_scroll_metrics(page)).get("scrollY", 0))
    except Exception:
        last_scroll_y = -1

    nav_keywords = ("list", "grid", "stills", "motion", "culture", "information", "journal")
    close_check_interval = 2

    while (time.monotonic() - start_time) * 1000 < total_time_ms:
        round_index += 1
        elapsed_ms = (time.monotonic() - start_time) * 1000
        scroll_priority_mode = scroll_to_end and elapsed_ms >= (total_time_ms * 0.72)

        if round_index % close_check_interval == 0:
            try:
                cursor_pos, _ = await try_close_overlay(
                    page,
                    cursor_pos,
                    viewport_width,
                    viewport_height,
                )
            except Exception:
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass

        if entry_click_enabled and round_index % 3 == 1:
            try:
                cursor_pos, clicked, clicked_key = await try_click_entry_element(
                    page=page,
                    cursor_pos=cursor_pos,
                    viewport_width=viewport_width,
                    viewport_height=viewport_height,
                    clicked_keys=clicked_entry_keys,
                )
                if clicked and clicked_key:
                    clicked_entry_keys.add(clicked_key)
            except Exception as exc:
                if _is_nav_error(exc):
                    await _recover_after_nav(page)
                # Non-critical, continue

        scan_limit = max(40, max_targets if max_targets > 0 else 40)
        try:
            targets = await collect_interactive_targets(page, viewport_width, viewport_height, scan_limit)
        except Exception as exc:
            if _is_nav_error(exc):
                await _recover_after_nav(page)
                targets = []
            else:
                targets = []
        candidates = [item for item in targets if str(item.get("key", "")) not in visited_keys]

        current_url = str(page.url or "")
        safe_click_candidates = [
            item for item in candidates
            if is_safe_inpage_click_target(item, current_url, allow_internal_nav_click)
        ]

        top_nav_candidates = [
            item for item in safe_click_candidates
            if is_probable_top_nav_target(item, viewport_height)
            and nav_signature(item) not in clicked_nav_keys
        ]
        media_candidates = [
            item for item in safe_click_candidates
            if float(item.get("width", 0.0)) * float(item.get("height", 0.0)) >= 35000
            and float(item.get("y", viewport_height)) > viewport_height * 0.12
            and not is_probable_top_nav_target(item, viewport_height)
        ]

        target: Optional[Dict[str, Any]] = None
        if not scroll_priority_mode and inpage_click_enabled and top_nav_candidates and round_index <= 10:
            nav_pool = sorted(top_nav_candidates, key=target_sort_score, reverse=True)[: min(10, len(top_nav_candidates))]
            target = random.choice(nav_pool[: min(5, len(nav_pool))])
        elif not scroll_priority_mode and inpage_click_enabled and media_candidates and random.random() < 0.60:
            target = random.choice(media_candidates[: min(5, len(media_candidates))])
        elif not scroll_priority_mode and candidates and (max_targets <= 0 or hovered_count < max_targets):
            top_pool = sorted(candidates, key=target_sort_score, reverse=True)[:8]
            target = random.choice(top_pool[:3] if len(top_pool) >= 3 else top_pool)

        if not scroll_priority_mode and target is None and candidates:
            # Weighted fallback: повышаем шанс на элементы дальше от последних траекторий,
            # чтобы курсор не циклился в одной зоне и выглядел естественно.
            weighted_pool = sorted(candidates, key=target_sort_score, reverse=True)[: min(12, len(candidates))]
            if weighted_pool:
                weights: List[float] = []
                for item in weighted_pool:
                    base_score = max(1.0, float(item.get("score", 1.0)))
                    tx = float(item.get("x", viewport_width / 2))
                    ty = float(item.get("y", viewport_height / 2))
                    dist_from_cursor = math.hypot(tx - cursor_pos[0], ty - cursor_pos[1])
                    novelty = 1.0
                    if recent_points:
                        near_recent = min(math.hypot(tx - px, ty - py) for px, py in recent_points)
                        novelty = clamp(near_recent / max(viewport_width, viewport_height), 0.35, 1.35)
                    distance_factor = clamp(dist_from_cursor / max(viewport_width, viewport_height), 0.45, 1.25)
                    weights.append(base_score * novelty * distance_factor)

                try:
                    target = random.choices(weighted_pool, weights=weights, k=1)[0]
                except Exception:
                    target = weighted_pool[0]

        if target is not None:
            tx = float(target["x"])
            ty = float(target["y"])
            cursor_pos = await move_mouse_human_like(
                page,
                cursor_pos,
                (tx, ty),
                viewport_width,
                viewport_height,
                random.randint(260, 860),
            )
            await page.wait_for_timeout(random.randint(40, 120))

            # Небольшой "поиск" в границах элемента, чтобы надежнее триггерить hover на сложной верстке.
            jitter_radius_x = max(2.0, min(float(target.get("width", 0.0)) * 0.12, 14.0))
            jitter_radius_y = max(2.0, min(float(target.get("height", 0.0)) * 0.12, 12.0))
            jx = clamp(tx + random.uniform(-jitter_radius_x, jitter_radius_x), 1, viewport_width - 1)
            jy = clamp(ty + random.uniform(-jitter_radius_y, jitter_radius_y), 1, viewport_height - 1)
            await page.mouse.move(jx, jy)
            await page.wait_for_timeout(random.randint(24, 80))

            hover_delay = random.randint(hover_min_ms, hover_max_ms)
            await page.wait_for_timeout(hover_delay)

            target_key = str(target.get("key", ""))
            should_click = (
                inpage_click_enabled
                and target_key not in clicked_inpage_keys
                and is_safe_inpage_click_target(target, current_url, allow_internal_nav_click)
            )

            click_probability = inpage_click_probability
            if has_keyword(str(target.get("text", "")), nav_keywords):
                click_probability = max(click_probability, 0.88)
            if is_probable_top_nav_target(target, viewport_height):
                click_probability = max(click_probability, 0.92)
            if float(target.get("width", 0.0)) * float(target.get("height", 0.0)) >= 35000:
                click_probability = min(max(click_probability, 0.22), 0.42)

            # Кнопки действий в виджетах/калькуляторах кликаются с высоким приоритетом.
            widget_action_words = (
                "accept", "reset", "submit", "next", "confirm", "apply",
                "calculate", "done", "save", "select", "choose", "finish",
            )
            if has_keyword(str(target.get("text", "")), widget_action_words):
                click_probability = max(click_probability, 0.88)

            if should_click and random.random() < click_probability:
                before_url = str(page.url or "")
                await page.mouse.click(tx, ty, delay=random.randint(35, 110))
                await page.wait_for_timeout(random.randint(120, 300))
                clicked_inpage_keys.add(target_key)

                if is_probable_top_nav_target(target, viewport_height):
                    clicked_nav_keys.add(nav_signature(target))

                cursor_pos, _ = await try_close_overlay(
                    page,
                    cursor_pos,
                    viewport_width,
                    viewport_height,
                )

                after_url = str(page.url or "")
                if (
                    after_url != before_url
                    and is_navigation_like_href(after_url, before_url)
                    and not allow_internal_nav_click
                ):
                    logger.info("🖱️ Smart cursor: обнаружен переход, откатываемся назад")
                    try:
                        await page.go_back(wait_until="domcontentloaded", timeout=3000)
                        await page.wait_for_timeout(random.randint(250, 550))
                    except Exception:
                        pass
                elif after_url != before_url and allow_internal_nav_click:
                    logger.info("🖱️ Smart cursor: выполнен внутренний переход по интерактиву")

            hovered_count += 1
            visited_keys.add(target_key)
            recent_points.append((tx, ty))
            if len(recent_points) > 6:
                recent_points.pop(0)

        if scroll_to_end:
            await perform_smooth_scroll(
                page,
                viewport_height,
                scroll_speed_factor,
                scroll_pause_min_ms,
                scroll_pause_max_ms,
            )

            try:
                metrics = await get_scroll_metrics(page)
                current_scroll_y = int(metrics.get("scrollY", last_scroll_y))
                at_bottom = bool(metrics.get("atBottom", False))
            except Exception:
                current_scroll_y = last_scroll_y
                at_bottom = False

            if current_scroll_y <= last_scroll_y + 3:
                stagnant_scroll_rounds += 1
            else:
                stagnant_scroll_rounds = 0

            if stagnant_scroll_rounds >= 2:
                try:
                    await page.keyboard.press("PageDown")
                    await page.wait_for_timeout(random.randint(max(20, scroll_pause_min_ms), max(40, scroll_pause_max_ms + 60)))
                except Exception:
                    pass

            last_scroll_y = max(last_scroll_y, current_scroll_y)
            if at_bottom:
                bottom_stable_rounds += 1
            else:
                bottom_stable_rounds = 0

            if bottom_stable_rounds >= bottom_stable_rounds_required and round_index >= 8:
                logger.info("🧭 Smart cursor: достигнут конец страницы")
                break

    if scroll_to_end:
        try:
            reached_end = await force_scroll_to_page_end(
                page,
                viewport_height,
                scroll_speed_factor,
                scroll_pause_min_ms,
                scroll_pause_max_ms,
                scroll_finish_timeout_ms,
            )
            if reached_end:
                logger.info("🧭 Smart cursor: финальным проходом достигнут конец страницы")
            else:
                logger.warning("⚠️ Smart cursor: не удалось гарантированно дойти до конца страницы в отведенный таймаут")
        except Exception as exc:
            if _is_nav_error(exc):
                logger.warning("⚠️ Smart cursor: навигация при финальном скролле")
                await _recover_after_nav(page)
            else:
                logger.warning(f"⚠️ Smart cursor: ошибка при финальном скролле: {exc}")

    logger.info(f"🧭 Smart cursor: обработано интерактивных целей {hovered_count}")
    return hovered_count


async def main():
    """Главная функция для рендеринга веб-сайта на сервере с Xvfb и FFmpeg видеозаписью."""
    browser = None
    try:
        # Получение конфигурации из переменных окружения
        output_path = os.getenv('OUTPUT_PATH', 'output')
        target_url = os.getenv('TARGET_URL', 'https://www.gsproductions.co.za/')
        viewport_width = int(os.getenv('VIEWPORT_WIDTH', '1920'))
        viewport_height = int(os.getenv('VIEWPORT_HEIGHT', '1080'))
        render_timeout = int(os.getenv('RENDER_TIMEOUT', '5000'))
        load_timeout = int(os.getenv('LOAD_TIMEOUT', '60000'))
        smart_cursor_enabled = env_bool('SMART_CURSOR_ENABLED', True)
        smart_cursor_timeout = int(os.getenv('SMART_CURSOR_TIMEOUT', '45000'))
        smart_cursor_max_targets = int(os.getenv('SMART_CURSOR_MAX_TARGETS', '24'))
        hover_min_ms = int(os.getenv('SMART_CURSOR_HOVER_MIN_MS', '450'))
        hover_max_ms = int(os.getenv('SMART_CURSOR_HOVER_MAX_MS', '1300'))
        entry_click_enabled = env_bool('SMART_CURSOR_ENTRY_CLICK_ENABLED', True)
        entry_click_attempts = int(os.getenv('SMART_CURSOR_ENTRY_CLICK_ATTEMPTS', '2'))
        scroll_to_end = env_bool('SMART_CURSOR_SCROLL_TO_END', True)
        bottom_stable_rounds_required = int(os.getenv('SMART_CURSOR_BOTTOM_STABLE_ROUNDS', '3'))
        scroll_speed_factor = float(os.getenv('SMART_CURSOR_SCROLL_SPEED', '1.4'))
        scroll_pause_min_ms = int(os.getenv('SMART_CURSOR_SCROLL_PAUSE_MIN_MS', '25'))
        scroll_pause_max_ms = int(os.getenv('SMART_CURSOR_SCROLL_PAUSE_MAX_MS', '70'))
        scroll_finish_timeout_ms = int(os.getenv('SMART_CURSOR_SCROLL_FINISH_TIMEOUT_MS', '22000'))
        nav_tabs_visit_enabled = env_bool('SMART_CURSOR_NAV_TABS_VISIT_ENABLED', True)
        nav_tabs_max_visits = int(os.getenv('SMART_CURSOR_NAV_TABS_MAX_VISITS', '10'))
        nav_tab_scroll_timeout_ms = int(os.getenv('SMART_CURSOR_NAV_TAB_SCROLL_TIMEOUT_MS', '17000'))
        inpage_click_enabled = env_bool('SMART_CURSOR_INPAGE_CLICK_ENABLED', True)
        inpage_click_probability = float(os.getenv('SMART_CURSOR_INPAGE_CLICK_PROBABILITY', '0.28'))
        allow_internal_nav_click = env_bool('SMART_CURSOR_ALLOW_INTERNAL_NAV_CLICK', True)
        browser_fullscreen = env_bool('BROWSER_FULLSCREEN', True)
        browser_app_mode = env_bool('BROWSER_APP_MODE', True)

        if hover_max_ms < hover_min_ms:
            hover_max_ms = hover_min_ms

        entry_click_attempts = max(0, min(entry_click_attempts, 4))
        bottom_stable_rounds_required = max(1, min(bottom_stable_rounds_required, 8))
        scroll_speed_factor = clamp(scroll_speed_factor, 0.6, 2.5)
        scroll_pause_min_ms = max(10, min(scroll_pause_min_ms, 400))
        scroll_pause_max_ms = max(scroll_pause_min_ms + 5, min(scroll_pause_max_ms, 800))
        scroll_finish_timeout_ms = max(0, min(scroll_finish_timeout_ms, 90000))
        nav_tabs_max_visits = max(0, min(nav_tabs_max_visits, 18))
        nav_tab_scroll_timeout_ms = max(1500, min(nav_tab_scroll_timeout_ms, 60000))
        inpage_click_probability = clamp(inpage_click_probability, 0.0, 1.0)
        
        logger.info("🚀 Запуск VPVoAe Web Renderer")
        logger.info(f"Target URL: {target_url}")
        logger.info(f"Viewport: {viewport_width}x{viewport_height}")
        logger.info(f"Display: {os.getenv('DISPLAY', ':99')}")
        logger.info("📹 Video recording: ENABLED (запись идёт параллельно)")
        logger.info(f"🧭 Smart cursor: {'ENABLED' if smart_cursor_enabled else 'DISABLED'}")
        logger.info(f"🖥️ Browser fullscreen: {'ENABLED' if browser_fullscreen else 'DISABLED'}")
        logger.info(f"🧱 Browser app mode: {'ENABLED' if browser_app_mode else 'DISABLED'}")
        
        async with async_playwright() as p:
            logger.info("🌐 Запуск браузера на виртуальном дисплее...")
            browser_args = [
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
                '--no-sandbox',
                '--disable-extensions',
                '--disable-web-resources',
                '--kiosk',
                '--start-fullscreen',
                '--start-maximized',
                '--window-position=0,0',
                f'--window-size={viewport_width},{viewport_height}',
                '--hide-crash-restore-bubble',
                '--disable-infobars',
            ]
            if browser_app_mode:
                browser_args.append('--app=data:,')

            browser = await p.chromium.launch(
                headless=False,
                args=browser_args,
            )
            
            context = await browser.new_context(
                viewport={'width': viewport_width, 'height': viewport_height},
                device_scale_factor=1
            )
            page = await context.new_page()

            logger.info(f"📄 Открываем целевой сайт: {target_url}")
            # Ждем, пока перестанут подгружаться новые ресурсы (networkidle)
            try:
                await page.goto(
                    target_url,
                    wait_until="networkidle",
                    timeout=load_timeout
                )
                logger.info("✅ Сайт загружен успешно")
            except asyncio.TimeoutError:
                logger.warning(f"⏱️  Таймаут при загрузке ({load_timeout}ms), продолжаем...")

            if browser_fullscreen:
                try:
                    await page.bring_to_front()
                    await page.wait_for_timeout(150)
                    await page.keyboard.press("F11")
                    await page.wait_for_timeout(450)
                except Exception:
                    logger.warning("⚠️ Не удалось переключить браузер в fullscreen")

            logger.info(f"⏳ Ожидание отрисовки WebGL анимаций ({render_timeout}ms)...")
            # Сайт тяжелый, даем время на полный рендер WebGL
            await page.wait_for_timeout(render_timeout)

            if smart_cursor_enabled and smart_cursor_timeout > 0:
                logger.info(
                    f"🧭 Smart cursor активирован: budget={smart_cursor_timeout}ms, max_targets={smart_cursor_max_targets}, scroll_to_end={scroll_to_end}"
                )
                await run_smart_cursor(
                    page=page,
                    viewport_width=viewport_width,
                    viewport_height=viewport_height,
                    total_time_ms=smart_cursor_timeout,
                    max_targets=smart_cursor_max_targets,
                    hover_min_ms=hover_min_ms,
                    hover_max_ms=hover_max_ms,
                    entry_click_enabled=entry_click_enabled,
                    entry_click_attempts=entry_click_attempts,
                    scroll_to_end=scroll_to_end,
                    bottom_stable_rounds_required=bottom_stable_rounds_required,
                    scroll_speed_factor=scroll_speed_factor,
                    scroll_pause_min_ms=scroll_pause_min_ms,
                    scroll_pause_max_ms=scroll_pause_max_ms,
                    scroll_finish_timeout_ms=scroll_finish_timeout_ms,
                    nav_tabs_visit_enabled=nav_tabs_visit_enabled,
                    nav_tabs_max_visits=nav_tabs_max_visits,
                    nav_tab_scroll_timeout_ms=nav_tab_scroll_timeout_ms,
                    inpage_click_enabled=inpage_click_enabled,
                    inpage_click_probability=inpage_click_probability,
                    allow_internal_nav_click=allow_internal_nav_click,
                )
            else:
                logger.info("🧭 Smart cursor пропущен по конфигурации")

            logger.info("📸 Создание скриншота...")
            # Создаем директорию для результатов
            os.makedirs(output_path, exist_ok=True)
            
            # Сохраняем скриншот с временной меткой (без full_page для совместимости с Xvfb)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = os.path.join(output_path, f"screenshot_{timestamp}.png")
            await page.screenshot(path=screenshot_path, full_page=False, omit_background=False)
            
            # Также сохраняем последний скриншот для быстрого доступа
            latest_path = os.path.join(output_path, "screenshot_latest.png")
            await page.screenshot(path=latest_path, full_page=False, omit_background=False)

            await context.close()
            
            file_size = os.path.getsize(screenshot_path) / (1024 * 1024)  # MB
            logger.info(f"✅ Скриншот сохранен: {screenshot_path} ({file_size:.2f}MB)")
            logger.info(f"✅ Latest: {latest_path}")
            logger.info("✨ Рендеринг завершен успешно")
            logger.info("📹 FFmpeg продолжает запись (будет остановлен в entrypoint.sh)")
            sys.exit(0)

    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
        sys.exit(1)
    finally:
        if browser:
            try:
                await browser.close()
                logger.info("🛑 Браузер закрыт")
            except Exception as e:
                logger.warning(f"Ошибка при закрытии браузера: {e}")


if __name__ == "__main__":
    logger.info("=" * 60)
    asyncio.run(main())
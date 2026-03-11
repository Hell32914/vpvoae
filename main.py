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


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value.strip())
    except Exception:
        return default


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = float(value.strip())
    except Exception:
        return default
    if not math.isfinite(parsed):
        return default
    return parsed


def clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(value, max_value))


def normalized_site_host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").strip().lower().strip(".")
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def is_same_site_url(candidate_url: str, allowed_url: str) -> bool:
    if not candidate_url or not allowed_url:
        return False
    try:
        candidate_parts = urlparse(candidate_url)
    except Exception:
        return False
    if candidate_parts.scheme not in {"http", "https"}:
        return False
    candidate_host = normalized_site_host(candidate_url)
    allowed_host = normalized_site_host(allowed_url)
    return bool(candidate_host and allowed_host and candidate_host == allowed_host)


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
                'summary',
                '[class*="review"]',
                '[class*="testimonial"]',
                '[class*="feedback"]',
                '[class*="rating"]',
                '[class*="comment"]',
                '[class*="quote"]',
                '[class*="opinion"]'
            ];

            const pool = new Set();

            function addIfElement(node) {
                if (node && node instanceof HTMLElement) {
                    pool.add(node);
                }
            }

            function hasTransition(style) {
                const raw = (style.transitionDuration || '').toString();
                if (!raw) return false;
                for (const part of raw.split(',')) {
                    const token = part.trim().toLowerCase();
                    if (!token) continue;
                    if (token.endsWith('ms')) {
                        if (Number.parseFloat(token) > 0.1) return true;
                        continue;
                    }
                    if (token.endsWith('s')) {
                        if (Number.parseFloat(token) > 0.001) return true;
                        continue;
                    }
                }
                return false;
            }

            function isSurfaceHoverCandidate(el, rect, style) {
                const tag = el.tagName.toLowerCase();
                const cls = (el.className || '').toString().toLowerCase();
                const idPart = (el.id || '').toString().toLowerCase();
                const attrHint = [
                    el.getAttribute('data-hover') || '',
                    el.getAttribute('data-cursor') || '',
                    el.getAttribute('aria-label') || '',
                ].join(' ').toLowerCase();
                const hint = `${cls} ${idPart} ${attrHint}`;

                const looksLikeScene = (
                    tag === 'canvas'
                    || tag === 'video'
                    || tag === 'model-viewer'
                    || /webgl|three|scene|hero|parallax|interactive|stage|viewer|model|experience/.test(hint)
                );

                const largeEnough = (
                    rect.width >= viewportWidth * 0.18
                    && rect.height >= viewportHeight * 0.16
                );

                const motionSignals = (
                    hasTransition(style)
                    || ((style.animationName || '').toLowerCase() !== 'none')
                    || ((style.willChange || '').toLowerCase().includes('transform'))
                    || ((style.transform || '').toLowerCase() !== 'none')
                    || ((style.cursor || '').toLowerCase() === 'pointer')
                    || ((style.cursor || '').toLowerCase() === 'grab')
                    || ((style.cursor || '').toLowerCase() === 'crosshair')
                );

                return (looksLikeScene && rect.width >= 80 && rect.height >= 50) || (largeEnough && motionSignals);
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

                const visibleLeft = Math.max(0, rect.left);
                const visibleTop = Math.max(0, rect.top);
                const visibleRight = Math.min(viewportWidth, rect.right);
                const visibleBottom = Math.min(viewportHeight, rect.bottom);
                const visibleWidth = Math.max(0, visibleRight - visibleLeft);
                const visibleHeight = Math.max(0, visibleBottom - visibleTop);
                const visibleArea = visibleWidth * visibleHeight;
                const totalArea = Math.max(1, rect.width * rect.height);
                const visibleRatio = visibleArea / totalArea;
                if (visibleRatio < 0.16) continue;

                const cx = rect.left + rect.width / 2;
                const cy = rect.top + rect.height / 2;
                if (!Number.isFinite(cx) || !Number.isFinite(cy)) continue;

                const samplePoints = [
                    [cx, cy],
                    [rect.left + rect.width * 0.28, rect.top + rect.height * 0.5],
                    [rect.left + rect.width * 0.72, rect.top + rect.height * 0.5],
                    [rect.left + rect.width * 0.5, rect.top + rect.height * 0.3],
                    [rect.left + rect.width * 0.5, rect.top + rect.height * 0.7],
                ];

                let sampleCount = 0;
                let clearCount = 0;
                for (const [px, py] of samplePoints) {
                    if (px < 0 || py < 0 || px > viewportWidth || py > viewportHeight) continue;
                    sampleCount += 1;
                    const topNode = document.elementFromPoint(px, py);
                    if (
                        topNode
                        && topNode instanceof Element
                        && (el === topNode || el.contains(topNode) || topNode.contains(el))
                    ) {
                        clearCount += 1;
                    }
                }

                const visibilityClarity = sampleCount > 0 ? (clearCount / sampleCount) : 0;
                if (visibilityClarity < 0.34) continue;

                const area = Math.min(rect.width * rect.height, 12000);
                const distFromCenter = Math.hypot(cx - viewportWidth / 2, cy - viewportHeight / 2);
                const centerScore = Math.max(0, 1 - distFromCenter / (diag * 0.55));
                const edgeDist = Math.min(cx, cy, viewportWidth - cx, viewportHeight - cy);
                const edgePenalty = edgeDist < 8 ? 40 : 0;
                const pointerBoost = style.cursor === 'pointer' ? 120 : 0;
                const tagBoost = ({ button: 170, a: 130, input: 110, select: 110, textarea: 100 })[el.tagName.toLowerCase()] || 80;

                const text = (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 40);
                const key = elementKey(el, cx, cy, text);
                let href = el instanceof HTMLAnchorElement ? (el.getAttribute('href') || '') : '';
                if (!href) { let p = el.parentElement; for (let d = 0; p && d < 4; d++, p = p.parentElement) { if (p instanceof HTMLAnchorElement) { href = p.getAttribute('href') || ''; break; } } }
                const dynamicBoost = /menu|nav|tab|card|tile|cta|action|play|pause|open|calc|form|wizard|step|option|choice|result/i.test((el.className || '').toString()) ? 30 : 0;
                const surfaceHover = isSurfaceHoverCandidate(el, rect, style);
                const surfaceBoost = surfaceHover ? 96 : 0;

                out.push({
                    x: Math.max(2, Math.min(viewportWidth - 2, cx)),
                    y: Math.max(2, Math.min(viewportHeight - 2, cy)),
                    width: rect.width,
                    height: rect.height,
                    score: area * 0.02 + centerScore * 100 + pointerBoost + tagBoost + dynamicBoost + surfaceBoost - edgePenalty,
                    key,
                    text,
                    href,
                    tag: el.tagName.toLowerCase(),
                    isSurfaceHover: surfaceHover,
                    visibleRatio,
                    visibilityClarity,
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
                let href = el instanceof HTMLAnchorElement ? (el.getAttribute('href') || '') : '';
                if (!href) { let p = el.parentElement; for (let d = 0; p && d < 4; d++, p = p.parentElement) { if (p instanceof HTMLAnchorElement) { href = p.getAttribute('href') || ''; break; } } }
                const z = toNumber(style.zIndex, 0);
                const area = rect.width * rect.height;
                const centerDist = Math.hypot(cx - viewportWidth / 2, cy - viewportHeight / 2);
                const centerScore = Math.max(0, 1 - centerDist / Math.hypot(viewportWidth / 2, viewportHeight / 2));

                const textToken = text.toLowerCase().replace(/\\s+/g, ' ').slice(0, 30);
                const hrefToken = href.toLowerCase().split('?')[0].slice(0, 40);
                const xBucket = Math.round(cx / 110);
                const yBucket = Math.round(cy / 110);
                const key = `${el.tagName.toLowerCase()}|${textToken}|${hrefToken}|${xBucket}:${yBucket}`;

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
    strong_entry_words = (
        "enter", "start", "explore", "begin", "launch", "proceed", "accept", "agree",
        "allow", "войти", "начать", "принять",
    )
    weak_entry_words = (
        "continue", "open", "go", "skip", "next", "ok", "продолж", "далее",
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
        text_compact = " ".join(text.split())
        href = str(candidate.get("href", "")).strip().lower()
        if has_keyword(text, purchase_words) or has_keyword(href, purchase_words):
            continue

        if href and is_navigation_like_href(href, current_url):
            # По требованию: не кликаем элементы, уводящие на другую страницу.
            continue

        candidate_score = float(candidate.get("score", 0.0))
        width = float(candidate.get("width", 0.0))
        height = float(candidate.get("height", 0.0))
        area = width * height
        x = float(candidate.get("x", 0.0))
        y = float(candidate.get("y", 0.0))
        z_index = float(candidate.get("zIndex", 0.0))
        center_dist = math.hypot(x - viewport_width / 2, y - viewport_height / 2)
        overlay_like = z_index >= 12
        compact_gate = area <= 18000 and width <= 220 and height <= 140
        centered_gate = center_dist <= math.hypot(viewport_width, viewport_height) * 0.24

        strong_entry_hint = has_keyword(text, strong_entry_words) or has_keyword(href, strong_entry_words)
        weak_entry_hint = has_keyword(text, weak_entry_words) or has_keyword(href, weak_entry_words)
        has_entry_hint = strong_entry_hint or (weak_entry_hint and centered_gate and (overlay_like or compact_gate))

        # Без явных признаков входа не кликаем контентные карточки/превью.
        if not has_entry_hint:
            short_text = len(text_compact) <= 20
            tiny_copy = len(text_compact.split()) <= 4
            if href:
                continue
            if not (
                candidate_score >= 110
                and short_text
                and tiny_copy
                and centered_gate
                and (overlay_like or compact_gate or not text_compact)
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

    try:
        before_state = await get_page_activity_snapshot(page)
    except Exception as exc:
        if _is_nav_error(exc):
            await _recover_after_nav(page)
            return cursor_pos, False, None
        raise

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

    try:
        after_state = await get_page_activity_snapshot(page)
        if not page_state_changed(before_state, after_state):
            logger.info("🧭 Smart cursor: entry-клик не изменил состояние страницы, завершаем фазу входа")
            return cursor_pos, False, None
    except Exception as exc:
        if _is_nav_error(exc):
            await _recover_after_nav(page)
        else:
            raise

    return cursor_pos, True, str(best.get("key", ""))


async def get_page_activity_snapshot(page: Any) -> Dict[str, Any]:
    """Легкий снимок состояния страницы для оценки, произошли ли изменения после клика."""
    _default = {
        "url": str(page.url or ""),
        "scrollY": 0,
        "height": 0,
        "title": "",
        "text": "",
        "media": "",
        "active": "",
    }
    try:
        result = await page.evaluate(
            """
            () => {
                const text = (document.body?.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 300);
                const viewportHeight = window.innerHeight || 0;
                const media = [];
                for (const node of document.querySelectorAll('img, video, canvas, [style*="background-image"]')) {
                    if (!(node instanceof Element)) continue;
                    const rect = node.getBoundingClientRect();
                    if (rect.width < 24 || rect.height < 24) continue;
                    if (rect.bottom < 0 || rect.top > viewportHeight) continue;

                    let signature = node.tagName.toLowerCase();
                    if (node instanceof HTMLImageElement) {
                        signature += ':' + ((node.currentSrc || node.src || '').split('?')[0].slice(-80));
                    } else if (node instanceof HTMLVideoElement) {
                        signature += ':' + ((node.currentSrc || node.getAttribute('src') || '').split('?')[0].slice(-80));
                    } else if (node instanceof HTMLElement) {
                        const bg = node.style.backgroundImage || window.getComputedStyle(node).backgroundImage || '';
                        signature += ':' + bg.slice(0, 80);
                    }
                    media.push(signature);
                    if (media.length >= 8) break;
                }

                const active = [];
                for (const node of document.querySelectorAll('[aria-current="page"], [aria-current="true"], [aria-selected="true"], .active, .is-active, .swiper-slide-active, .slick-active, .w--current, .w--open')) {
                    if (!(node instanceof HTMLElement)) continue;
                    const rect = node.getBoundingClientRect();
                    if (rect.bottom < 0 || rect.top > viewportHeight) continue;
                    const label = (node.innerText || node.getAttribute('aria-label') || node.id || node.className || '').toString().trim().replace(/\\s+/g, ' ').slice(0, 60);
                    active.push(label);
                    if (active.length >= 8) break;
                }

                return {
                    url: location.href,
                    scrollY: window.scrollY,
                    height: document.documentElement?.scrollHeight || 0,
                    title: document.title || '',
                    text,
                    media: media.join('|'),
                    active: active.join('|')
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
                const toInt = (value, fallback = 0) => {
                    const n = Number(value);
                    return Number.isFinite(n) ? Math.round(n) : fallback;
                };

                const viewportHeight = toInt(window.innerHeight || 0, 0);
                const scrollingEl = document.scrollingElement || document.documentElement || document.body;

                let scrollY = Math.max(
                    toInt(window.scrollY || window.pageYOffset || 0, 0),
                    toInt(scrollingEl?.scrollTop || 0, 0),
                );

                let maxScroll = Math.max(
                    0,
                    toInt((scrollingEl?.scrollHeight || 0) - viewportHeight, 0),
                );

                const candidates = [];
                if (document.body) candidates.push(document.body);
                for (const el of document.querySelectorAll('[data-scroll-container], [data-scroll], .smooth-scroll, .scroll-container, .lenis, .lenis-root, main')) {
                    if (el instanceof HTMLElement) candidates.push(el);
                }

                const seen = new Set();
                for (const el of candidates) {
                    if (!(el instanceof HTMLElement) || seen.has(el)) continue;
                    seen.add(el);

                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    const elementHeight = Math.max(
                        toInt(el.scrollHeight || 0, 0),
                        toInt(el.offsetHeight || 0, 0),
                        toInt(rect.height || 0, 0),
                    );
                    if (elementHeight < viewportHeight + 40) continue;

                    const nativeMax = Math.max(0, toInt((el.scrollHeight || 0) - (el.clientHeight || viewportHeight), 0));
                    if (nativeMax > 0) {
                        scrollY = Math.max(scrollY, toInt(el.scrollTop || 0, 0));
                        maxScroll = Math.max(maxScroll, nativeMax);
                    }

                    const transformedTop = Math.max(0, toInt(-(rect.top || 0), 0));
                    const transformedMax = Math.max(0, elementHeight - viewportHeight);
                    const hasTransform = (
                        ((style.transform || '').toLowerCase() !== 'none')
                        || ((style.willChange || '').toLowerCase().includes('transform'))
                    );

                    if (hasTransform || transformedTop > 0) {
                        scrollY = Math.max(scrollY, transformedTop);
                        maxScroll = Math.max(maxScroll, transformedMax);
                    }
                }

                const atBottom = maxScroll <= 4 ? true : scrollY >= (maxScroll - 6);
                return {
                    scrollY,
                    maxScroll,
                    atBottom,
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
        or str(before.get("media", "")) != str(after.get("media", ""))
        or str(before.get("active", "")) != str(after.get("active", ""))
    )


def has_keyword(value: str, keywords: Tuple[str, ...]) -> bool:
    low = value.lower()
    if any(word in low for word in keywords):
        return True
    # Дополнительно проверяем по отдельным токенам (login vs log in, signup vs sign up)
    tokens = set(low.split())
    collapsed = low.replace(" ", "")
    for word in keywords:
        if " " in word:
            # Для многословных ключей проверяем склеенный вариант: "sign up" -> "signup"
            if word.replace(" ", "") in collapsed:
                return True
        elif word in tokens:
            return True
    return False


def _to_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except Exception:
        return default
    if not math.isfinite(parsed):
        return default
    return parsed


def target_visible_ratio(target: Dict[str, Any], fallback: float = 1.0) -> float:
    raw = _to_float(target.get("visibleRatio"), -1.0)
    if raw < 0.0:
        return clamp(fallback, 0.0, 1.0)
    return clamp(raw, 0.0, 1.0)


def target_visibility_clarity(target: Dict[str, Any], fallback: float = 1.0) -> float:
    raw = _to_float(target.get("visibilityClarity"), -1.0)
    if raw < 0.0:
        return clamp(fallback, 0.0, 1.0)
    return clamp(raw, 0.0, 1.0)


def is_target_clearly_visible(
    target: Dict[str, Any],
    viewport_width: int,
    viewport_height: int,
    min_visible_ratio: float,
    min_visibility_clarity: float,
    resolved_y: Optional[float] = None,
    edge_margin_x_factor: float = 0.03,
    edge_margin_y_factor: float = 0.05,
) -> bool:
    x = _to_float(target.get("x"), viewport_width * 0.5)
    y = resolved_y if resolved_y is not None else _to_float(target.get("y"), viewport_height * 0.5)
    width = _to_float(target.get("width"), 0.0)
    height = _to_float(target.get("height"), 0.0)

    if width < 10.0 or height < 10.0:
        return False

    edge_margin_x = max(6.0, viewport_width * max(0.0, edge_margin_x_factor))
    edge_margin_y = max(6.0, viewport_height * max(0.0, edge_margin_y_factor))
    if x < edge_margin_x or x > (viewport_width - edge_margin_x):
        return False
    if y < edge_margin_y or y > (viewport_height - edge_margin_y):
        return False

    fallback_visibility = 1.0 if (0.0 <= x <= viewport_width and 0.0 <= y <= viewport_height) else 0.0
    visible_ratio = target_visible_ratio(target, fallback=fallback_visibility)
    visibility_clarity = target_visibility_clarity(target, fallback=fallback_visibility)

    if visible_ratio < clamp(min_visible_ratio, 0.0, 1.0):
        return False
    if visibility_clarity < clamp(min_visibility_clarity, 0.0, 1.0):
        return False

    return True


def is_navigation_like_href(href: str, current_url: str) -> bool:
    clean = href.strip().lower()
    if not clean or clean.startswith("#") or clean.startswith("javascript:"):
        return False

    resolved = urljoin(current_url, href)
    current_parts = urlparse(current_url)
    resolved_parts = urlparse(resolved)

    if resolved_parts.scheme not in {"http", "https"}:
        return False

    if not is_same_site_url(resolved, current_url):
        return True

    # Если путь/параметры отличаются - это переход на другую страницу.
    return (resolved_parts.path != current_parts.path) or (resolved_parts.query != current_parts.query)


def is_external_href(href: str, current_url: str, allowed_url: Optional[str] = None) -> bool:
    clean = href.strip().lower()
    if not clean or clean.startswith("#") or clean.startswith("javascript:"):
        return False

    resolved = urljoin(current_url, href)
    resolved_parts = urlparse(resolved)

    if resolved_parts.scheme not in {"http", "https"}:
        return True

    reference_url = allowed_url or current_url
    return not is_same_site_url(resolved, reference_url)


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
    if not is_same_site_url(resolved, current_url):
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


async def restore_page_location(
    page: Any,
    restore_url: str,
    restore_scroll_y: Optional[float] = None,
    timeout: int = 15000,
) -> bool:
    """Возвращает страницу на заданный URL и, если нужно, восстанавливает скролл."""
    if not restore_url:
        return False

    restored = False
    try:
        await page.goto(restore_url, wait_until="domcontentloaded", timeout=timeout)
        restored = True
    except Exception:
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=min(timeout, 9000))
            restored = str(page.url or "") == restore_url
        except Exception:
            restored = False

    if not restored:
        return False

    await _recover_after_nav(page, timeout=timeout)
    if restore_scroll_y is not None:
        try:
            await page.evaluate(
                """(top) => window.scrollTo({ top, left: 0, behavior: 'auto' })""",
                int(max(0.0, restore_scroll_y)),
            )
            await page.wait_for_timeout(random.randint(220, 420))
        except Exception:
            pass
    return True


async def ensure_page_within_allowed_site(
    page: Any,
    allowed_url: str,
    fallback_url: Optional[str] = None,
    fallback_scroll_y: Optional[float] = None,
    timeout: int = 15000,
) -> bool:
    """Возвращает страницу обратно на разрешённый сайт, если навигация ушла наружу."""
    current_url = str(page.url or "")
    if not current_url or is_same_site_url(current_url, allowed_url):
        return False

    restore_url = fallback_url if fallback_url and is_same_site_url(fallback_url, allowed_url) else allowed_url
    logger.warning(
        "⛔ Site guard: обнаружен уход с разрешённого сайта, "
        f"текущий URL={current_url}, восстанавливаем {restore_url}"
    )
    return await restore_page_location(
        page,
        restore_url,
        restore_scroll_y=fallback_scroll_y,
        timeout=timeout,
    )


def is_safe_inpage_click_target(
    target: Dict[str, Any],
    current_url: str,
    allow_internal_nav_click: bool,
    allowed_url: Optional[str] = None,
) -> bool:
    """Разрешаем клик только по элементам, которые не уводят на другую страницу."""
    text = str(target.get("text", "")).strip().lower()
    href = str(target.get("href", "")).strip().lower()
    tag = str(target.get("tag", "")).strip().lower()

    if tag in {"input", "textarea", "select"}:
        return False

    blocked_action_words = (
        "buy", "shop", "cart", "checkout", "pricing", "price", "guide", "ebook", "course",
        "purchase", "subscribe", "plan", "membership", "donate", "book", "store", "order",
        "download", "install", "get started", "sign up", "signup", "sign in", "signin",
        "log in", "login", "register", "free trial", "try for free", "try free",
        "get app", "app store", "google play", "start free", "start trial",
        "get it", "launch", "deploy", "free", "trial", "demo",
    )
    if has_keyword(text, blocked_action_words) or has_keyword(href, blocked_action_words):
        return False

    if href and is_external_href(href, current_url, allowed_url=allowed_url):
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


def is_safe_nav_tab_target(target: Dict[str, Any], current_url: str, allowed_url: Optional[str] = None) -> bool:
    """Безопасная фильтрация top-nav вкладок: разрешаем внутренние разделы, но исключаем внешние/commerce/system."""
    text = str(target.get("text", "")).strip().lower()
    href = str(target.get("href", "")).strip().lower()
    tag = str(target.get("tag", "")).strip().lower()

    if tag in {"input", "textarea", "select"}:
        return False

    blocked_words = (
        "buy", "shop", "cart", "checkout", "pricing", "price", "guide", "ebook", "course",
        "purchase", "subscribe", "plan", "membership", "donate", "book", "store", "order",
        "login", "signin", "sign in", "log in", "account", "privacy", "terms", "cookie",
        "download", "install", "sign up", "signup", "register", "free trial", "try for free",
        "try free", "get app", "app store", "google play", "start free", "start trial",
        "get it", "launch", "deploy", "free", "trial", "demo",
    )
    if has_keyword(text, blocked_words) or has_keyword(href, blocked_words):
        return False

    if href and is_external_href(href, current_url, allowed_url=allowed_url):
        return False

    width = float(target.get("width", 0.0))
    height = float(target.get("height", 0.0))
    score = float(target.get("score", 0.0))
    if width < 14 or height < 10:
        return False

    # Для меню ослабляем порог относительно generic click target.
    return score >= 35


def is_probable_top_nav_target(target: Dict[str, Any], viewport_height: int) -> bool:
    """Универсально определяет элементы верхней навигации (вкладки/пункты меню)."""
    y = float(target.get("y", viewport_height))
    width = float(target.get("width", 0.0))
    height = float(target.get("height", 0.0))
    text = str(target.get("text", "")).strip().lower()
    href = str(target.get("href", "")).strip().lower()
    tag = str(target.get("tag", "")).strip().lower()
    visible_ratio = target_visible_ratio(target, fallback=1.0)
    visibility_clarity = target_visibility_clarity(target, fallback=1.0)

    if y > viewport_height * 0.24:
        return False

    if width < 28 or width > 420 or height < 10 or height > 120:
        return False

    if visible_ratio < 0.42 or visibility_clarity < 0.42:
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


def strict_target_family_key(target: Dict[str, Any]) -> str:
    """Стабильный ключ цели для STRICT режима, устойчивый к мелкому дрожанию координат/текста."""
    tag = str(target.get("tag", "")).strip().lower()
    text_raw = str(target.get("text", "")).strip().lower()
    text_norm = " ".join("".join(ch if ch.isalnum() else " " for ch in text_raw).split())
    href = str(target.get("href", "")).strip().lower().split("?")[0]

    x = float(target.get("x", 0.0))
    abs_y = float(target.get("absY", target.get("y", 0.0)))
    width = float(target.get("width", 0.0))
    height = float(target.get("height", 0.0))

    x_bucket = int(max(0.0, x) / 92)
    y_bucket = int(max(0.0, abs_y) / 240)
    area_bucket = int(max(0.0, width * height) / 8500)

    marker = ""
    if bool(target.get("isSurfaceHover", False)):
        marker += "S"
    if bool(target.get("isHoverText", False)):
        marker += "H"

    # Для hover-эффектов в глубине страницы сохраняем привязку к y-bucket,
    # чтобы не терять похожие эффекты в разных секциях.
    hover_depth_bucket = ""
    if marker and abs_y >= 360:
        hover_depth_bucket = f"|y{y_bucket}"

    # Для подписанных/ссылочных контролов intentionally ослабляем зависимость от y,
    # чтобы не повторять одни и те же fixed/sticky hover-цели при прокрутке.
    if text_norm or href:
        return f"{tag}|{text_norm[:26]}|{href[:36]}|x{x_bucket}{hover_depth_bucket}|a{area_bucket}|{marker}"

    return f"{tag}|x{x_bucket}|y{y_bucket}|a{area_bucket}|{marker}"


def interaction_text(target: Dict[str, Any]) -> str:
    text_raw = str(target.get("text", "")).strip().lower()
    return " ".join("".join(ch if ch.isalnum() else " " for ch in text_raw).split())


def is_probable_repeatable_control(target: Dict[str, Any]) -> bool:
    """Определяет локальные контролы, которые разумно нажимать несколько раз подряд."""
    text = interaction_text(target)
    href = str(target.get("href", "")).strip().lower()
    tag = str(target.get("tag", "")).strip().lower()
    width = _to_float(target.get("width"), 0.0)
    height = _to_float(target.get("height"), 0.0)
    area = max(0.0, width * height)

    if href and not href.startswith("#") and not href.startswith("javascript:"):
        return False

    repeat_words = (
        "next", "prev", "previous", "back", "more", "slide", "slider", "carousel",
        "play", "pause", "resume", "forward", "rewind", "fwd", "step", "continue",
        "again", "retry", "tab", "left", "right", "expand", "collapse", "open", "close",
    )
    button_like = tag in {"button", "summary"} or not href
    compact = area <= 26000 and width <= 260 and height <= 170
    terse = len(text) <= 18

    if has_keyword(text, repeat_words) and button_like:
        return True

    if button_like and compact and (not text or terse):
        return True

    return False


def target_progress_y(target: Dict[str, Any]) -> float:
    return float(target.get("absY", target.get("y", 0.0)))


def shortlist_progress_targets(targets: List[Dict[str, Any]], band_px: float, limit: int) -> List[Dict[str, Any]]:
    """Сохраняет последовательное движение сверху вниз вместо случайных скачков по viewport."""
    if not targets:
        return []

    ordered = sorted(
        targets,
        key=lambda item: (
            target_progress_y(item),
            float(item.get("x", 0.0)),
            -target_sort_score(item),
        ),
    )
    anchor_y = target_progress_y(ordered[0])
    band_limit = max(40.0, float(band_px))
    band = [item for item in ordered if target_progress_y(item) <= anchor_y + band_limit]
    return band[: max(1, limit)]


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


async def try_close_chat_widgets(page: Any) -> bool:
    """
    Скрывает всплывающие чат-виджеты (Intercom, Drift, Crisp, Tawk, Freshchat, Potion и т.д.).
    Возвращает True, если что-то было скрыто.
    """
    try:
        hidden = await page.evaluate(
            """
            () => {
                const chatSelectors = [
                    '#intercom-container',
                    '#intercom-frame',
                    'iframe[name*="intercom"]',
                    '[class*="intercom-"]',
                    '#drift-widget',
                    '#drift-frame',
                    'iframe[id*="drift"]',
                    '#crisp-chatbox',
                    '[class*="crisp-client"]',
                    '#tawk-widget-container',
                    'iframe[title*="tawk"]',
                    '[class*="tawk-"]',
                    '#fc_frame',
                    '#freshchat-container',
                    '[id*="freshchat"]',
                    '[class*="freshchat"]',
                    '[class*="chat-widget"]',
                    '[class*="chat-bubble"]',
                    '[class*="support-chat"]',
                    '[class*="livechat-widget"]',
                    '[class*="helpcrunch"]',
                    '[class*="tidio-"]',
                    '#tidio-chat',
                    '[class*="zsiq"]',
                    '#hubspot-messages-iframe-container',
                    '[class*="fb-customerchat"]',
                    '[class*="potion-"]',
                    '[id*="potion"]',
                    '[class*="chatbot"]',
                    '[class*="chat-popup"]',
                    '[class*="chat-container"]',
                ];

                let count = 0;
                for (const sel of chatSelectors) {
                    try {
                        const nodes = document.querySelectorAll(sel);
                        for (const el of nodes) {
                            if (!(el instanceof HTMLElement)) continue;
                            const st = window.getComputedStyle(el);
                            if (st.display === 'none') continue;
                            el.style.setProperty('display', 'none', 'important');
                            count++;
                        }
                    } catch(_) {}
                }

                // Also hide iframes that look like chat widgets (small, bottom-right)
                const iframes = document.querySelectorAll('iframe');
                for (const iframe of iframes) {
                    if (!(iframe instanceof HTMLElement)) continue;
                    const rect = iframe.getBoundingClientRect();
                    const st = window.getComputedStyle(iframe);
                    if (st.display === 'none') continue;
                    const vw = window.innerWidth;
                    const vh = window.innerHeight;
                    // Chat widgets are typically <500px wide, <700px tall, anchored bottom-right
                    if (
                        rect.width > 0 && rect.width <= 500 &&
                        rect.height > 0 && rect.height <= 700 &&
                        rect.right >= vw * 0.55 &&
                        rect.bottom >= vh * 0.40 &&
                        (iframe.src || '').match(/intercom|drift|crisp|tawk|freshchat|hubspot|tidio|helpcrunch|livechat|potion|chatbot|chat/i)
                    ) {
                        iframe.style.setProperty('display', 'none', 'important');
                        count++;
                    }
                }

                return count;
            }
            """
        )
        if hidden and hidden > 0:
            logger.info(f"🗑️ Скрыто чат-виджетов: {hidden}")
            return True
    except Exception as exc:
        if _is_nav_error(exc):
            await _recover_after_nav(page)
        return False
    return False


async def perform_followup_click_sequence(
    page: Any,
    cursor_pos: Tuple[float, float],
    anchor_x: float,
    anchor_y: float,
    anchor_key: str,
    anchor_family: str,
    viewport_width: int,
    viewport_height: int,
    clicked_keys: Set[str],
    clicked_families: Set[str],
    hover_min_ms: int,
    hover_max_ms: int,
    max_followup_steps: int,
    radius_factor: float,
    min_visible_ratio: float,
    min_visibility_clarity: float,
    allow_internal_nav_click: bool,
    hover_click_words: Tuple[str, ...],
    widget_action_words: Tuple[str, ...],
    visited_keys: Optional[Set[str]] = None,
    visited_families: Optional[Set[str]] = None,
    recent_interactions: Optional[List[Tuple[float, float, float, int]]] = None,
    round_index: int = 0,
    scroll_y: float = 0.0,
    allowed_url: Optional[str] = None,
) -> Tuple[Tuple[float, float], int]:
    """Пробует цепочку соседних кликов для локальных step-by-step интерактивов."""
    if max_followup_steps <= 0:
        return cursor_pos, 0

    performed = 0
    started_at = time.monotonic()
    current_x = float(anchor_x)
    current_y = float(anchor_y)
    current_key = str(anchor_key)
    current_family = str(anchor_family)
    family_repeat_counts: Dict[str, int] = {}
    family_last_changed: Dict[str, bool] = {}
    max_repeatable_clicks = 4

    for step_index in range(max_followup_steps):
        if allowed_url:
            await ensure_page_within_allowed_site(
                page,
                allowed_url,
                fallback_url=allowed_url,
                timeout=12000,
            )
        if (time.monotonic() - started_at) * 1000 >= 2600:
            break
        wait_min = max(90, int(hover_min_ms * 0.40))
        wait_max = max(wait_min + 30, int(hover_max_ms * 0.70))
        try:
            await page.wait_for_timeout(random.randint(wait_min, wait_max))
            nearby_targets = await collect_interactive_targets(page, viewport_width, viewport_height, 44)
        except Exception as exc:
            if _is_nav_error(exc):
                await _recover_after_nav(page)
            break

        radius_limit = max(
            120.0,
            min(viewport_width, viewport_height) * (radius_factor + step_index * 0.05),
        )
        radius_limit = min(radius_limit, max(viewport_width, viewport_height) * 0.58)

        ranked: List[Tuple[Tuple[int, int, int, int, int, float, float], Dict[str, Any]]] = []
        current_url = str(page.url or "")

        for item in nearby_targets:
            item_key = str(item.get("key", ""))
            item_family = strict_target_family_key(item)
            item_repeatable = is_probable_repeatable_control(item)
            item_repeat_count = family_repeat_counts.get(item_family, 0)
            same_family_allowed = item_repeatable and family_last_changed.get(item_family, False) and item_repeat_count < max_repeatable_clicks

            if not item_key:
                continue
            if (item_key in clicked_keys or item_key == current_key) and not same_family_allowed:
                continue
            if (item_family in clicked_families or item_family == current_family) and not same_family_allowed:
                continue

            if not is_safe_inpage_click_target(item, current_url, allow_internal_nav_click, allowed_url=allowed_url):
                continue

            ix = clamp(_to_float(item.get("x"), current_x), 2, viewport_width - 2)
            iy = clamp(_to_float(item.get("y"), current_y), 2, viewport_height - 2)
            if not is_target_clearly_visible(
                item,
                viewport_width,
                viewport_height,
                min_visible_ratio=min_visible_ratio,
                min_visibility_clarity=min_visibility_clarity,
                resolved_y=iy,
            ):
                continue

            dist = math.hypot(ix - current_x, iy - current_y)
            if dist > radius_limit:
                continue

            text_low = str(item.get("text", "")).strip().lower()
            hint = has_keyword(text_low, hover_click_words) or has_keyword(text_low, widget_action_words)
            item_tag = str(item.get("tag", "")).strip().lower()
            compact = (_to_float(item.get("width"), 0.0) * _to_float(item.get("height"), 0.0)) <= 32000
            hover_like = bool(item.get("isHoverText", False)) or bool(item.get("isSurfaceHover", False))
            button_like = item_tag in {"button", "summary"}
            passive_hover = hover_like and not hint and not button_like
            continue_same_control = item_repeatable and item_family == current_family and family_last_changed.get(item_family, False)

            rank_tuple = (
                0 if hint else 1,
                0 if button_like else 1,
                0 if compact else 1,
                0 if continue_same_control else 1,
                1 if passive_hover else 0,
                dist,
                -target_sort_score(item),
            )
            ranked.append((rank_tuple, item))

        if not ranked:
            break

        ranked.sort(key=lambda entry: entry[0])
        follow_target = ranked[0][1]
        follow_key = str(follow_target.get("key", ""))
        follow_family = strict_target_family_key(follow_target)
        follow_x = clamp(_to_float(follow_target.get("x"), current_x), 2, viewport_width - 2)
        follow_y = clamp(_to_float(follow_target.get("y"), current_y), 2, viewport_height - 2)

        try:
            before_snapshot = await get_page_activity_snapshot(page)
            cursor_pos = await move_mouse_human_like(
                page=page,
                start=cursor_pos,
                end=(follow_x, follow_y),
                viewport_width=viewport_width,
                viewport_height=viewport_height,
                duration_ms=random.randint(150, 420),
            )
            await page.wait_for_timeout(random.randint(wait_min, wait_max))
            before_url = str(page.url or "")
            await page.mouse.click(follow_x, follow_y, delay=random.randint(30, 95))
            await page.wait_for_timeout(random.randint(130, 320))
            after_url = str(page.url or "")
            after_snapshot = await get_page_activity_snapshot(page)
        except Exception as exc:
            if _is_nav_error(exc):
                await _recover_after_nav(page)
                break
            continue

        local_changed = page_state_changed(before_snapshot, after_snapshot)
        effective_change = local_changed or (after_url != before_url)
        family_repeat_counts[follow_family] = family_repeat_counts.get(follow_family, 0) + 1
        family_last_changed[follow_family] = local_changed

        if effective_change:
            clicked_keys.add(follow_key)
            clicked_families.add(follow_family)
            if visited_keys is not None:
                visited_keys.add(follow_key)
            if visited_families is not None:
                visited_families.add(follow_family)

        if recent_interactions is not None:
            recent_interactions.append((follow_x, follow_y, float(scroll_y + follow_y), round_index))
            if len(recent_interactions) > 20:
                recent_interactions.pop(0)

        if effective_change:
            performed += 1

        navigated_offsite = bool(allowed_url) and after_url and not is_same_site_url(after_url, allowed_url)
        navigated_internally = after_url != before_url and is_navigation_like_href(after_url, before_url)

        if navigated_offsite and allowed_url:
            await ensure_page_within_allowed_site(
                page,
                allowed_url,
                fallback_url=before_url,
                fallback_scroll_y=scroll_y,
                timeout=12000,
            )
            break

        if navigated_internally and not allow_internal_nav_click:
            await restore_page_location(
                page,
                before_url,
                restore_scroll_y=scroll_y,
                timeout=12000,
            )
            break

        current_x = follow_x
        current_y = follow_y
        current_key = follow_key
        current_family = follow_family

    return cursor_pos, performed


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
        await page.wait_for_timeout(random.randint(scroll_pause_min_ms, scroll_pause_max_ms))


async def force_scroll_progress(page: Any, viewport_height: int) -> None:
    """Форсирует продвижение скролла на сайтах с нестандартным smooth-scroll."""
    delta = max(80, int(viewport_height * 0.9))
    try:
        await page.mouse.wheel(0, delta)
        await page.wait_for_timeout(random.randint(45, 110))
    except Exception:
        pass

    try:
        await page.keyboard.press("PageDown")
        await page.wait_for_timeout(random.randint(28, 80))
    except Exception:
        pass

    try:
        await page.evaluate(
            """
            (delta) => {
                const targets = [
                    document.scrollingElement,
                    document.documentElement,
                    document.body,
                    ...document.querySelectorAll('[data-scroll-container], [data-scroll], .smooth-scroll, .scroll-container, .lenis, .lenis-root, main'),
                ];

                const seen = new Set();
                for (const node of targets) {
                    if (!(node instanceof HTMLElement) || seen.has(node)) continue;
                    seen.add(node);

                    try {
                        if ((node.scrollHeight || 0) > (node.clientHeight || 0) + 4) {
                            node.scrollBy({ top: delta, left: 0, behavior: 'auto' });
                        }
                    } catch {}
                }

                try { window.scrollBy({ top: delta, left: 0, behavior: 'auto' }); } catch {}

                try {
                    window.dispatchEvent(new WheelEvent('wheel', {
                        deltaY: delta,
                        bubbles: true,
                        cancelable: true,
                    }));
                } catch {}

                return true;
            }
            """,
            delta,
        )
    except Exception:
        pass


async def collect_document_interaction_targets(
    page: Any,
    viewport_width: int,
    viewport_height: int,
    limit: int,
) -> List[Dict[str, Any]]:
    """Сканирует DOM до скролла и возвращает интерактивные цели с абсолютной Y-координатой."""
    return await page.evaluate(
        """
        ({ viewportWidth, viewportHeight, limit }) => {
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
                '[class*="char"]',
                '[class*="letter"]',
                '[data-char]',
                '[data-letter]',
                '[role="tab"]',
                '[role="tablist"]',
                '[role="tabpanel"]',
                '[data-slide]',
                '[data-index]',
                '[class*="gallery"]',
                '[class*="slider"]',
                '[class*="carousel"]',
                '[class*="swiper"]',
                '[class*="lightbox"]',
                '[class*="phone"]',
                '[class*="mockup"]',
                '[class*="device"]',
                '[class*="screen"]',
                '[class*="preview"]',
                'img',
                'li',
                'h1',
                'h2',
                'h3',
                'h4',
                'span',
                'video',
                'canvas',
                'summary',
                '[class*="review"]',
                '[class*="testimonial"]',
                '[class*="feedback"]',
                '[class*="rating"]',
                '[class*="comment"]',
                '[class*="quote"]',
                '[class*="opinion"]',
            ];

            const pool = new Set();
            for (const node of document.querySelectorAll(selectors.join(','))) {
                if (node instanceof HTMLElement) pool.add(node);
            }

            const clampValue = (value, minValue, maxValue) => Math.max(minValue, Math.min(maxValue, value));
            const doc = document.documentElement;
            const body = document.body;
            const docHeight = Math.max(
                doc?.scrollHeight || 0,
                body?.scrollHeight || 0,
                viewportHeight,
            );

            function elementKey(el, absX, absY, text) {
                const parts = [];
                let cur = el;
                let depth = 0;
                while (cur && cur instanceof HTMLElement && depth < 5) {
                    const cls = (cur.className || '').toString().trim().split(/\\s+/).slice(0, 2).join('.');
                    parts.push(`${cur.tagName.toLowerCase()}${cur.id ? '#' + cur.id : ''}${cls ? '.' + cls : ''}`);
                    cur = cur.parentElement;
                    depth += 1;
                }
                return `${parts.join('>')}|${Math.round(absX)}:${Math.round(absY)}|${text.slice(0, 30)}`;
            }

            function hasTransition(style) {
                const raw = (style.transitionDuration || '').toString();
                if (!raw) return false;
                for (const part of raw.split(',')) {
                    const token = part.trim().toLowerCase();
                    if (!token) continue;
                    if (token.endsWith('ms')) {
                        if (Number.parseFloat(token) > 0.1) return true;
                        continue;
                    }
                    if (token.endsWith('s')) {
                        if (Number.parseFloat(token) > 0.001) return true;
                        continue;
                    }
                }
                return false;
            }

            function hasMotionSignals(style) {
                return (
                    hasTransition(style)
                    || ((style.transitionProperty || '').toLowerCase().includes('transform'))
                    || ((style.transitionProperty || '').toLowerCase().includes('all'))
                    || ((style.animationName || '').toLowerCase() !== 'none')
                    || ((style.willChange || '').toLowerCase().includes('transform'))
                    || ((style.willChange || '').toLowerCase().includes('filter'))
                    || ((style.willChange || '').toLowerCase().includes('opacity'))
                    || ((style.transform || '').toLowerCase() !== 'none')
                );
            }

            function isInteractiveNode(el, tag) {
                if (['a', 'button', 'input', 'select', 'textarea', 'summary'].includes(tag)) {
                    return true;
                }
                return (
                    el.hasAttribute('onclick')
                    || el.hasAttribute('data-action')
                    || el.hasAttribute('aria-controls')
                    || el.getAttribute('role') === 'button'
                    || el.getAttribute('role') === 'link'
                    || el.getAttribute('role') === 'menuitem'
                );
            }

            function isSurfaceHoverCandidate(el, tag, rect, style) {
                const cls = (el.className || '').toString().toLowerCase();
                const idPart = (el.id || '').toString().toLowerCase();
                const attrHint = [
                    el.getAttribute('data-hover') || '',
                    el.getAttribute('data-cursor') || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || '',
                ].join(' ').toLowerCase();
                const hint = `${cls} ${idPart} ${attrHint}`;

                // Отсекаем чисто текстовые контейнеры (p, div, section без явных hover-подсказок)
                const textOnlyTags = ['p', 'span', 'article', 'blockquote', 'li', 'ul', 'ol', 'figcaption', 'label'];
                if (textOnlyTags.includes(tag)
                    && !hint.match(/hover|cursor|interactive|parallax|card|slide|flip|tilt|zoom|magnif/)
                    && (style.cursor || 'auto') === 'auto') {
                    return false;
                }

                const looksLikeScene = (
                    tag === 'canvas'
                    || tag === 'video'
                    || tag === 'model-viewer'
                    || /webgl|three|scene|parallax|interactive|stage|viewer|model|experience|showcase|gallery|carousel|slider|swiper|phone|device|mockup|preview|lightbox/.test(hint)
                );

                const largeEnough = (
                    rect.width >= viewportWidth * 0.16
                    && rect.height >= viewportHeight * 0.14
                );

                const cursorKind = (style.cursor || '').toLowerCase();
                const cursorInteractive = (
                    cursorKind === 'pointer'
                    || cursorKind === 'grab'
                    || cursorKind === 'crosshair'
                    || cursorKind === 'move'
                );

                // Требуем ЯВНЫЕ признаки интерактивности: transition/animation + cursor ИЛИ scene-маркер
                const hasStrong = hasMotionSignals(style) && cursorInteractive;
                return (looksLikeScene && rect.width >= 80 && rect.height >= 50)
                    || (largeEnough && hasStrong);
            }

            function isHoverEffectTextCandidate(el, tag, textNoWs, rect, style) {
                if (textNoWs.length < 1 || textNoWs.length > 84) return false;

                const cls = (el.className || '').toString().toLowerCase();
                const idPart = (el.id || '').toString().toLowerCase();
                const attrHint = [
                    el.getAttribute('data-hover') || '',
                    el.getAttribute('data-char') || '',
                    el.getAttribute('data-letter') || '',
                    el.getAttribute('aria-label') || '',
                ].join(' ').toLowerCase();
                const hint = `${cls} ${idPart} ${attrHint}`;

                const hasMotionStyle = hasMotionSignals(style);

                // Явные маркеры hover-текста (split-text анимации, per-char эффекты)
                const hasExplicitHoverHint = (
                    hint.includes('hover')
                    || hint.includes('char')
                    || hint.includes('letter')
                    || hint.includes('glyph')
                    || hint.includes('split')
                    || hint.includes('scramble')
                    || hint.includes('magnetic')
                );

                const looksMicroText = (
                    ['span', 'em', 'strong', 'i', 'b', 'a'].includes(tag)
                    && textNoWs.length <= 10
                    && rect.width <= 320
                );

                const likelyHoverZone = (
                    rect.width >= 120
                    && rect.height >= 16
                    && rect.width <= viewportWidth * 0.95
                    && rect.height <= viewportHeight * 0.5
                );

                if (!likelyHoverZone) return false;

                // Headings: только если есть motion-style ИЛИ явный hover-хинт
                if (/^h[1-6]$/.test(tag)) {
                    return hasMotionStyle || hasExplicitHoverHint;
                }

                // Микро-текст (ссылки, span): требуем и motion, и cursor=pointer
                if (looksMicroText) {
                    return hasMotionStyle && style.cursor === 'pointer';
                }

                // Явный hover-хинт в атрибутах → доверяем
                if (hasExplicitHoverHint && hasMotionStyle) return true;

                return false;
            }

            // Дополнительный grid-scan для сайтов без семантических DOM-меток.
            const scanCols = 11;
            const scanRows = 7;
            for (let r = 0; r < scanRows; r++) {
                for (let c = 0; c < scanCols; c++) {
                    const x = ((c + 0.5) / scanCols) * viewportWidth;
                    const y = ((r + 0.5) / scanRows) * viewportHeight;
                    const stack = document.elementsFromPoint(x, y) || [];
                    for (const node of stack.slice(0, 7)) {
                        if (!(node instanceof HTMLElement)) continue;
                        const tag = node.tagName.toLowerCase();
                        const rect = node.getBoundingClientRect();
                        if (rect.width < 10 || rect.height < 10) continue;

                        const style = window.getComputedStyle(node);
                        if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || '1') < 0.05) {
                            continue;
                        }
                        if (style.pointerEvents === 'none') continue;

                        if (
                            isInteractiveNode(node, tag)
                            || isSurfaceHoverCandidate(node, tag, rect, style)
                            || ((style.cursor || '').toLowerCase() === 'pointer' && hasMotionSignals(style))
                        ) {
                            pool.add(node);
                        }
                    }
                }
            }

            const out = [];
            for (const el of pool) {
                if (!(el instanceof HTMLElement)) continue;

                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || '1') < 0.05) {
                    continue;
                }
                if (style.pointerEvents === 'none') continue;

                const rect = el.getBoundingClientRect();
                if (rect.width < 10 || rect.height < 10) continue;

                const visibleLeft = Math.max(0, rect.left);
                const visibleTop = Math.max(0, rect.top);
                const visibleRight = Math.min(viewportWidth, rect.right);
                const visibleBottom = Math.min(viewportHeight, rect.bottom);
                const visibleWidth = Math.max(0, visibleRight - visibleLeft);
                const visibleHeight = Math.max(0, visibleBottom - visibleTop);
                const visibleArea = visibleWidth * visibleHeight;
                const totalArea = Math.max(1, rect.width * rect.height);
                const visibleRatio = visibleArea / totalArea;
                if (visibleRatio < 0.12) continue;

                const samplePoints = [
                    [rect.left + rect.width * 0.5, rect.top + rect.height * 0.5],
                    [rect.left + rect.width * 0.25, rect.top + rect.height * 0.5],
                    [rect.left + rect.width * 0.75, rect.top + rect.height * 0.5],
                    [rect.left + rect.width * 0.5, rect.top + rect.height * 0.28],
                    [rect.left + rect.width * 0.5, rect.top + rect.height * 0.72],
                ];
                let sampleCount = 0;
                let clearCount = 0;
                for (const [px, py] of samplePoints) {
                    if (px < 0 || py < 0 || px > viewportWidth || py > viewportHeight) continue;
                    sampleCount += 1;
                    const topNode = document.elementFromPoint(px, py);
                    if (
                        topNode
                        && topNode instanceof Element
                        && (el === topNode || el.contains(topNode) || topNode.contains(el))
                    ) {
                        clearCount += 1;
                    }
                }
                const visibilityClarity = sampleCount > 0 ? (clearCount / sampleCount) : 0;
                if (visibilityClarity < 0.30) continue;

                const absX = rect.left + (window.scrollX || 0) + rect.width / 2;
                const absY = rect.top + (window.scrollY || 0) + rect.height / 2;
                if (!Number.isFinite(absX) || !Number.isFinite(absY)) continue;
                if (absY < -120 || absY > docHeight + 260) continue;

                const text = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim().replace(/\\s+/g, ' ').slice(0, 84);
                const textNoWs = text.replace(/\\s+/g, '');
                let href = el instanceof HTMLAnchorElement ? (el.getAttribute('href') || '') : '';
                if (!href) { let p = el.parentElement; for (let d = 0; p && d < 4; d++, p = p.parentElement) { if (p instanceof HTMLAnchorElement) { href = p.getAttribute('href') || ''; break; } } }
                const tag = el.tagName.toLowerCase();

                const interactive = isInteractiveNode(el, tag);
                const hoverText = isHoverEffectTextCandidate(el, tag, textNoWs, rect, style);
                const surfaceHover = isSurfaceHoverCandidate(el, tag, rect, style);
                if (!interactive && !hoverText && !surfaceHover) continue;

                const key = elementKey(el, absX, absY, text);

                const pointerBoost = style.cursor === 'pointer' ? 90 : 0;
                const tagBoost = ({ button: 100, a: 90, input: 70, select: 70, textarea: 65, video: 55, canvas: 55 })[tag] || 45;
                const textBoost = text.length > 0 ? 24 : 0;
                const areaBoost = Math.min(rect.width * rect.height, 12000) * 0.01;
                const hoverBoost = hoverText ? 82 : 0;
                const surfaceBoost = surfaceHover ? 106 : 0;

                out.push({
                    key,
                    x: clampValue(absX, 2, viewportWidth - 2),
                    absY: Math.max(1, absY),
                    width: rect.width,
                    height: rect.height,
                    score: pointerBoost + tagBoost + textBoost + areaBoost + hoverBoost + surfaceBoost,
                    text,
                    href,
                    tag,
                    isHoverText: hoverText,
                    isSurfaceHover: surfaceHover,
                    visibleRatio,
                    visibilityClarity,
                });
            }

            out.sort((a, b) => (a.absY - b.absY) || (b.score - a.score));
            return out.slice(0, Math.max(limit, 1));
        }
        """,
        {
            "viewportWidth": viewport_width,
            "viewportHeight": viewport_height,
            "limit": limit,
        },
    )


async def run_strict_top_to_bottom_pass(
    page: Any,
    cursor_pos: Tuple[float, float],
    viewport_width: int,
    viewport_height: int,
    total_time_ms: int,
    max_targets: int,
    hover_min_ms: int,
    hover_max_ms: int,
    bottom_stable_rounds_required: int,
    scroll_speed_factor: float,
    scroll_pause_min_ms: int,
    scroll_pause_max_ms: int,
    inpage_click_enabled: bool,
    inpage_click_probability: float,
    scroll_finish_timeout_ms: int,
    require_bottom: bool,
    require_bottom_max_ms: int,
    strict_allow_clicks: bool,
    bottom_debug: bool,
    hover_visible_ratio: float,
    click_visible_ratio: float,
    min_visibility_clarity: float,
    click_sequence_max_steps: int,
    click_sequence_radius_factor: float,
    allow_internal_nav_click: bool,
    site_url: str,
    strict_interactions_per_scroll: int,
    stall_timeout_ms: int,
) -> Tuple[Tuple[float, float], int, bool]:
    """Однонаправленный проход сверху вниз: приоритет hover-эффектам, без переходов на другие страницы."""
    hovered_count = 0
    reached_bottom = False
    visited_keys: Set[str] = set()
    visited_families: Set[str] = set()
    clicked_keys: Set[str] = set()
    clicked_families: Set[str] = set()
    # Трекер уже показанных hover-зон (absY, absY_end) чтобы не ходить курсором
    # по дочерним элементам той же карточки/блока.
    hovered_zones: List[Tuple[float, float, float, float]] = []  # (x, absY, x+w, absY+h)
    recent_interactions: List[Tuple[float, float, float, int]] = []
    analysis_targets: List[Dict[str, Any]] = []

    widget_action_words = (
        "accept", "reset", "submit", "next", "confirm", "apply",
        "calculate", "done", "save", "select", "choose", "finish",
    )
    hover_click_words = (
        "click", "tap", "press", "here", "open", "show", "reveal",
        "start", "play", "go", "next", "more", "toggle", "menu",
        "try", "view", "explore", "activate", "continue", "enter",
    )

    try:
        await page.evaluate("""() => window.scrollTo({ top: 0, left: 0, behavior: 'auto' })""")
        await page.wait_for_timeout(random.randint(180, 420))
    except Exception:
        pass

    started_at = time.monotonic()
    soft_budget_ms = max(8000, int(total_time_ms))
    hard_budget_ms = max(soft_budget_ms, int(require_bottom_max_ms) if require_bottom else soft_budget_ms)
    # Минимальная гарантированная длительность записи (30% бюджета).
    # Пока не прошло min_duration_ms — «дно» не завершает запись.
    min_duration_ms = max(8000, int(soft_budget_ms * 0.30))

    last_scroll_y = -1
    stagnant_rounds = 0
    bottom_stable_rounds = 0
    round_index = 0
    # Скрываем чат-виджеты, которые могли открыться при загрузке страницы
    try:
        await try_close_chat_widgets(page)
    except Exception:
        pass
    last_analysis_round = -1000
    # ── Быстрые тайминги для «поисковой» фазы (курсор летит к цели) ──
    search_move_min = 60
    search_move_max = 150
    search_dwell_min = 18
    search_dwell_max = 45
    # ── Тайминги для «витрины»: когда эффект обнаружен — показываем по-человечески ──
    micro_hover_min = max(40, int(hover_min_ms * 0.35))
    micro_hover_max = max(micro_hover_min + 20, int(hover_max_ms * 0.55))
    surface_hover_min = max(micro_hover_min + 30, int(hover_min_ms * 0.42))
    surface_hover_max = max(surface_hover_min + 35, int(hover_max_ms * 0.75))
    hover_showcase_min = max(240, int(hover_min_ms * 0.95))
    hover_showcase_max = max(hover_showcase_min + 90, int(hover_max_ms * 1.20))
    # ── Витринные (замедленные) move durations при показе найденного эффекта ──
    showcase_move_surface_min = 140
    showcase_move_surface_max = 340
    showcase_move_text_min = 180
    showcase_move_text_max = 480
    strict_interactions_per_scroll = max(1, min(int(strict_interactions_per_scroll), 4))
    stall_timeout_ms = max(3500, min(int(stall_timeout_ms), 90000))
    rounds_since_scroll = 0
    no_progress_rounds = 0
    interaction_pause_rounds = 0
    last_progress_at = time.monotonic()
    # ── Трекер секции: не более 10 секунд на один экран ──
    section_max_ms = 10000
    section_scroll_y_start = 0
    section_entered_at = time.monotonic()

    while True:
        elapsed_ms = (time.monotonic() - started_at) * 1000
        if elapsed_ms >= soft_budget_ms and (reached_bottom or not require_bottom):
            break
        if elapsed_ms >= hard_budget_ms:
            break

        round_index += 1
        interacted_this_round = False
        if interaction_pause_rounds > 0:
            interaction_pause_rounds -= 1

        await ensure_page_within_allowed_site(
            page,
            site_url,
            fallback_url=site_url,
            timeout=15000,
        )

        if round_index == 1 or (round_index - last_analysis_round) >= 8:
            try:
                analysis_targets = await collect_document_interaction_targets(
                    page=page,
                    viewport_width=viewport_width,
                    viewport_height=viewport_height,
                    limit=1200,
                )
                last_analysis_round = round_index
                if round_index == 1:
                    logger.info(f"🧭 Smart cursor: DOM анализ до скролла, найдено целей={len(analysis_targets)}")
            except Exception as exc:
                if _is_nav_error(exc):
                    await _recover_after_nav(page)
                    analysis_targets = []
                else:
                    raise

        try:
            metrics = await get_scroll_metrics(page)
            scroll_y_raw = int(metrics.get("scrollY", max(last_scroll_y, 0)))
            if bottom_debug and last_scroll_y >= 0 and scroll_y_raw + 8 < last_scroll_y:
                logger.info(
                    "🧭 Scroll rebound detected: "
                    f"raw={scroll_y_raw}, last={last_scroll_y}. Принудительно держим движение вниз"
                )
            scroll_y = max(scroll_y_raw, max(last_scroll_y, 0))
            at_bottom_now = bool(metrics.get("atBottom", False))
        except Exception as exc:
            if _is_nav_error(exc):
                await _recover_after_nav(page)
                scroll_y = max(last_scroll_y, 0)
                at_bottom_now = False
            else:
                raise

        can_interact = ((max_targets <= 0) or (hovered_count < max_targets)) and interaction_pause_rounds <= 0
        if can_interact and analysis_targets:
            viewport_top = scroll_y + int(viewport_height * 0.18)
            viewport_bottom = scroll_y + int(viewport_height * 0.82)
            current_url = str(page.url or "")

            candidates: List[Dict[str, Any]] = []
            for item in analysis_targets:
                item_key = str(item.get("key", ""))
                if not item_key or item_key in visited_keys:
                    continue

                item_abs_y = float(item.get("absY", 0.0))
                if not (viewport_top <= int(item_abs_y) <= viewport_bottom):
                    continue
                if is_probable_top_nav_target(item, viewport_height):
                    continue

                # ── Пропускаем элементы с внешними/навигационными ссылками ──
                item_href = str(item.get("href", "")).strip()
                if item_href and is_external_href(item_href, current_url, allowed_url=site_url):
                    continue
                if item_href and is_navigation_like_href(item_href, current_url) and not allow_internal_nav_click:
                    continue
                item_text_low = str(item.get("text", "")).strip().lower()
                _blocked_commercial = (
                    "buy", "shop", "cart", "checkout", "pricing", "price",
                    "purchase", "subscribe", "plan", "membership", "donate", "store", "order",
                    "download", "install", "sign up", "signup", "sign in", "signin",
                    "log in", "login", "register", "free trial", "try for free", "try free",
                    "get app", "app store", "google play", "start free", "start trial",
                    "free", "trial", "demo",
                )
                if has_keyword(item_text_low, _blocked_commercial):
                    continue

                item_family = strict_target_family_key(item)
                if item_family in visited_families:
                    continue

                item_x = float(item.get("x", viewport_width * 0.5))
                item_view_y = clamp(item_abs_y - scroll_y, 2, viewport_height - 2)
                if not is_target_clearly_visible(
                    item,
                    viewport_width,
                    viewport_height,
                    min_visible_ratio=hover_visible_ratio,
                    min_visibility_clarity=min_visibility_clarity,
                    resolved_y=item_view_y,
                ):
                    continue
                recently_repeated = False
                for px, py, p_abs_y, p_round in recent_interactions:
                    if round_index - p_round > 7:
                        continue
                    if abs(item_abs_y - p_abs_y) > max(140.0, viewport_height * 0.22):
                        continue
                    if math.hypot(item_x - px, item_view_y - py) <= max(76.0, min(viewport_width, viewport_height) * 0.10):
                        recently_repeated = True
                        break
                if recently_repeated:
                    continue

                candidates.append(item)

            if candidates:
                priority_click_candidates: List[Dict[str, Any]] = []
                for item in candidates:
                    txt = str(item.get("text", "")).strip().lower()
                    href = str(item.get("href", "")).strip().lower()
                    tag = str(item.get("tag", "")).strip().lower()
                    width = float(item.get("width", 0.0))
                    height = float(item.get("height", 0.0))
                    area = width * height
                    item_view_y = clamp(float(item.get("absY", scroll_y + viewport_height * 0.5)) - scroll_y, 2, viewport_height - 2)

                    action_hint = has_keyword(txt, hover_click_words) or has_keyword(txt, widget_action_words)
                    button_like = tag in {"button", "summary"}
                    no_nav_href = (not href) or href.startswith("#") or href.startswith("javascript:")
                    compact = area <= 30000 and width <= 260 and height <= 170
                    repeatable_control = is_probable_repeatable_control(item)

                    looks_clickable_trigger = (
                        action_hint
                        or repeatable_control
                        or (button_like and compact)
                        or (no_nav_href and compact and txt and len(txt) <= 24)
                    )

                    if (
                        looks_clickable_trigger
                        and is_safe_inpage_click_target(
                            item,
                            current_url,
                            allow_internal_nav_click=allow_internal_nav_click,
                            allowed_url=site_url,
                        )
                        and is_target_clearly_visible(
                            item,
                            viewport_width,
                            viewport_height,
                            min_visible_ratio=click_visible_ratio,
                            min_visibility_clarity=min_visibility_clarity,
                            resolved_y=item_view_y,
                        )
                    ):
                        priority_click_candidates.append(item)

                # Фильтруем кандидатов, которые попадают в уже показанную hover-зону
                def _in_hovered_zone(it: Dict[str, Any]) -> bool:
                    ix = float(it.get("x", 0.0))
                    iy = float(it.get("absY", 0.0))
                    iw = float(it.get("width", 0.0))
                    ih = float(it.get("height", 0.0))
                    for zx1, zy1, zx2, zy2 in hovered_zones:
                        # Элемент полностью внутри зоны (дочерний элемент карточки)
                        if ix >= zx1 - 10 and iy >= zy1 - 10 and (ix + iw) <= zx2 + 10 and (iy + ih) <= zy2 + 10:
                            return True
                    return False

                hover_text_candidates = [item for item in candidates if bool(item.get("isHoverText", False)) and not _in_hovered_zone(item)]
                surface_hover_candidates = [item for item in candidates if bool(item.get("isSurfaceHover", False)) and not _in_hovered_zone(item)]
                any_hover_candidates = [
                    item for item in candidates
                    if (bool(item.get("isSurfaceHover", False)) or bool(item.get("isHoverText", False)))
                    and not _in_hovered_zone(item)
                ]

                # Приоритет: hover-элементы выше click-элементов.
                # Click-кандидаты только когда нет hover-целей.
                if surface_hover_candidates:
                    pool = shortlist_progress_targets(surface_hover_candidates, band_px=170.0, limit=min(6, len(surface_hover_candidates)))
                elif hover_text_candidates:
                    pool = shortlist_progress_targets(hover_text_candidates, band_px=170.0, limit=min(6, len(hover_text_candidates)))
                elif any_hover_candidates:
                    pool = shortlist_progress_targets(any_hover_candidates, band_px=180.0, limit=min(6, len(any_hover_candidates)))
                elif inpage_click_enabled and priority_click_candidates:
                    pool = shortlist_progress_targets(priority_click_candidates, band_px=150.0, limit=min(6, len(priority_click_candidates)))
                else:
                    pool = None

                if pool is None:
                    candidates = []

            if candidates and pool:
                target = pool[0]

                # ── Подкрутка скролла, чтобы цель была видна в центральной зоне экрана ──
                _target_abs_y_raw = float(target.get("absY", scroll_y + viewport_height * 0.5))
                _ty_raw = _target_abs_y_raw - scroll_y
                _safe_top = viewport_height * 0.22
                _safe_bottom = viewport_height * 0.78
                if _ty_raw < _safe_top or _ty_raw > _safe_bottom:
                    # Цель слишком близко к краю экрана — подкручиваем, чтобы она стала по центру.
                    desired_scroll = max(0, int(_target_abs_y_raw - viewport_height * 0.45))
                    scroll_delta = desired_scroll - scroll_y
                    if abs(scroll_delta) > 30:
                        try:
                            _steps = random.randint(3, 5)
                            _step_size = int(scroll_delta / _steps)
                            for _si in range(_steps):
                                _d = _step_size + random.randint(-8, 8)
                                await page.mouse.wheel(0, _d)
                                await page.wait_for_timeout(random.randint(18, 45))
                            await page.wait_for_timeout(random.randint(40, 100))
                            _new_metrics = await get_scroll_metrics(page)
                            scroll_y = max(scroll_y, int(_new_metrics.get("scrollY", scroll_y)))
                        except Exception as _exc:
                            if _is_nav_error(_exc):
                                await _recover_after_nav(page)
                                continue

                tx = clamp(float(target.get("x", viewport_width * 0.5)), 2, viewport_width - 2)
                ty = clamp(float(target.get("absY", scroll_y + viewport_height * 0.5)) - scroll_y, 2, viewport_height - 2)
                target_abs_y = float(target.get("absY", scroll_y + ty))
                target_key = str(target.get("key", ""))
                target_family = strict_target_family_key(target)
                is_hover_text = bool(target.get("isHoverText", False))
                is_surface_hover = bool(target.get("isSurfaceHover", False))
                target_width = float(target.get("width", 0.0))
                target_height = float(target.get("height", 0.0))
                target_text = str(target.get("text", ""))
                target_text_low = target_text.strip().lower()
                target_href = str(target.get("href", "")).strip().lower()
                target_tag = str(target.get("tag", "")).strip().lower()

                # ── Определяем, безопасно ли кликать по этому элементу ──
                target_click_safe = (
                    inpage_click_enabled
                    and target_key not in clicked_keys
                    and target_family not in clicked_families
                    and is_safe_inpage_click_target(
                        target,
                        current_url,
                        allow_internal_nav_click=allow_internal_nav_click,
                        allowed_url=site_url,
                    )
                    and is_target_clearly_visible(
                        target,
                        viewport_width,
                        viewport_height,
                        min_visible_ratio=click_visible_ratio,
                        min_visibility_clarity=min_visibility_clarity,
                        resolved_y=ty,
                    )
                )

                # Если элемент не hover и не click-safe — пропускаем,
                # нет смысла двигать курсор к простому тексту.
                _is_any_hover = is_hover_text or is_surface_hover
                if not _is_any_hover and not target_click_safe:
                    visited_keys.add(target_key)
                    visited_families.add(target_family)
                    continue

                # ── Шаг 0: перемещаем курсор к цели (быстрая «поисковая» скорость) ──
                try:
                    before_snapshot = await get_page_activity_snapshot(page)
                except Exception as exc:
                    if _is_nav_error(exc):
                        await _recover_after_nav(page)
                        continue
                    before_snapshot = {"url": str(page.url or ""), "scrollY": scroll_y, "height": 0, "title": "", "text": "", "media": "", "active": ""}

                try:
                    cursor_pos = await move_mouse_human_like(
                        page=page,
                        start=cursor_pos,
                        end=(tx, ty),
                        viewport_width=viewport_width,
                        viewport_height=viewport_height,
                        duration_ms=random.randint(search_move_min, search_move_max),
                    )
                    await page.wait_for_timeout(random.randint(search_dwell_min, search_dwell_max))
                except Exception as exc:
                    if _is_nav_error(exc):
                        await _recover_after_nav(page)
                        continue
                    continue

                click_changed = False
                hover_changed = False
                followup_total = 0

                # ════════════════════════════════════════════════════════
                # CLICK-FIRST: сначала пробуем клик, если элемент безопасен
                # ════════════════════════════════════════════════════════
                if target_click_safe:
                    before_url = str(page.url or "")
                    before_scroll = scroll_y
                    try:
                        await page.mouse.click(tx, ty, delay=random.randint(25, 70))
                        await page.wait_for_timeout(random.randint(150, 320))
                    except Exception as exc:
                        if _is_nav_error(exc):
                            await _recover_after_nav(page)
                            visited_keys.add(target_key)
                            visited_families.add(target_family)
                            hovered_count += 1
                            interacted_this_round = True
                            continue

                    after_url = str(page.url or "")
                    try:
                        after_click_snapshot = await get_page_activity_snapshot(page)
                        click_changed = page_state_changed(before_snapshot, after_click_snapshot)
                    except Exception as exc:
                        if _is_nav_error(exc):
                            await _recover_after_nav(page)
                            visited_keys.add(target_key)
                            visited_families.add(target_family)
                            hovered_count += 1
                            interacted_this_round = True
                            continue
                        click_changed = False

                    effective_click_change = click_changed or (after_url != before_url)

                    # ── Защита от ухода на внешний сайт ──
                    navigated_offsite = after_url and not is_same_site_url(after_url, site_url)
                    navigated_internally = after_url != before_url and is_navigation_like_href(after_url, before_url)
                    if navigated_offsite:
                        logger.warning("⛔ Smart cursor: пойман внешний переход, возвращаемся")
                        await ensure_page_within_allowed_site(
                            page, site_url, fallback_url=before_url,
                            fallback_scroll_y=before_scroll, timeout=15000,
                        )
                        visited_keys.add(target_key)
                        visited_families.add(target_family)
                        clicked_keys.add(target_key)
                        clicked_families.add(target_family)
                        hovered_count += 1
                        interacted_this_round = True
                        no_progress_rounds = 0
                        last_progress_at = time.monotonic()
                        recent_interactions.append((tx, ty, target_abs_y, round_index))
                        if len(recent_interactions) > 20:
                            recent_interactions.pop(0)
                        continue
                    elif navigated_internally and not allow_internal_nav_click:
                        logger.info("🖱️ Smart cursor: пойман внутренний переход, откатываемся")
                        await restore_page_location(page, before_url, restore_scroll_y=before_scroll, timeout=15000)
                        visited_keys.add(target_key)
                        visited_families.add(target_family)
                        clicked_keys.add(target_key)
                        clicked_families.add(target_family)
                        hovered_count += 1
                        interacted_this_round = True
                        no_progress_rounds = 0
                        last_progress_at = time.monotonic()
                        recent_interactions.append((tx, ty, target_abs_y, round_index))
                        if len(recent_interactions) > 20:
                            recent_interactions.pop(0)
                        continue

                    if effective_click_change:
                        clicked_keys.add(target_key)
                        clicked_families.add(target_family)
                        logger.info(f"🖱️ Smart cursor: клик изменил состояние '{target_text[:30]}'")
                        # Даём время на анимацию после изменения.
                        await page.wait_for_timeout(random.randint(hover_showcase_min, hover_showcase_max))

                        # ── Анализ новых элементов, появившихся после клика ──
                        try:
                            new_targets = await collect_interactive_targets(page, viewport_width, viewport_height, 30)
                        except Exception as exc:
                            if _is_nav_error(exc):
                                await _recover_after_nav(page)
                            new_targets = []

                        deep_clicks = 0
                        deep_limit = max(2, min(click_sequence_max_steps + 2, 6))
                        for new_item in new_targets:
                            if deep_clicks >= deep_limit:
                                break
                            ni_key = str(new_item.get("key", ""))
                            ni_family = strict_target_family_key(new_item)
                            if not ni_key or ni_key in clicked_keys or ni_key in visited_keys:
                                continue
                            if ni_family in clicked_families or ni_family in visited_families:
                                continue
                            ni_x = clamp(_to_float(new_item.get("x"), tx), 2, viewport_width - 2)
                            ni_y = clamp(_to_float(new_item.get("y"), ty), 2, viewport_height - 2)
                            dist = math.hypot(ni_x - tx, ni_y - ty)
                            if dist > max(180.0, min(viewport_width, viewport_height) * 0.45):
                                continue
                            if not is_safe_inpage_click_target(
                                new_item, str(page.url or ""),
                                allow_internal_nav_click=allow_internal_nav_click,
                                allowed_url=site_url,
                            ):
                                continue
                            if not is_target_clearly_visible(
                                new_item, viewport_width, viewport_height,
                                min_visible_ratio=click_visible_ratio,
                                min_visibility_clarity=min_visibility_clarity,
                                resolved_y=ni_y,
                            ):
                                continue
                            try:
                                ni_before = await get_page_activity_snapshot(page)
                                cursor_pos = await move_mouse_human_like(
                                    page, cursor_pos, (ni_x, ni_y),
                                    viewport_width, viewport_height,
                                    random.randint(80, 200),
                                )
                                await page.wait_for_timeout(random.randint(30, 80))
                                await page.mouse.click(ni_x, ni_y, delay=random.randint(20, 65))
                                await page.wait_for_timeout(random.randint(150, 320))
                                ni_after = await get_page_activity_snapshot(page)
                                ni_changed = page_state_changed(ni_before, ni_after)
                            except Exception as exc:
                                if _is_nav_error(exc):
                                    await _recover_after_nav(page)
                                    break
                                continue

                            # Проверяем, не ушли ли мы на внешний/внутренний URL.
                            ni_after_url = str(page.url or "")
                            if ni_after_url and not is_same_site_url(ni_after_url, site_url):
                                await ensure_page_within_allowed_site(
                                    page, site_url, fallback_url=before_url,
                                    fallback_scroll_y=before_scroll, timeout=12000,
                                )
                                break
                            if ni_after_url != before_url and is_navigation_like_href(ni_after_url, before_url) and not allow_internal_nav_click:
                                await restore_page_location(page, before_url, restore_scroll_y=before_scroll, timeout=12000)
                                break

                            clicked_keys.add(ni_key)
                            clicked_families.add(ni_family)
                            visited_keys.add(ni_key)
                            visited_families.add(ni_family)
                            if ni_changed:
                                deep_clicks += 1
                                await page.wait_for_timeout(random.randint(180, 400))

                        followup_total = deep_clicks
                        if followup_total > 0:
                            logger.info(f"🖱️ Smart cursor: после клика найдено и нажато новых элементов: {followup_total}")

                        # Дополнительно пробуем цепочку perform_followup_click_sequence
                        if followup_total == 0:
                            followup_steps = max(2, min(click_sequence_max_steps + 2, 5))
                            cursor_pos, seq_count = await perform_followup_click_sequence(
                                page=page, cursor_pos=cursor_pos,
                                anchor_x=tx, anchor_y=ty,
                                anchor_key=target_key, anchor_family=target_family,
                                viewport_width=viewport_width, viewport_height=viewport_height,
                                clicked_keys=clicked_keys, clicked_families=clicked_families,
                                hover_min_ms=hover_min_ms, hover_max_ms=hover_max_ms,
                                max_followup_steps=followup_steps,
                                radius_factor=click_sequence_radius_factor,
                                min_visible_ratio=click_visible_ratio,
                                min_visibility_clarity=min_visibility_clarity,
                                allow_internal_nav_click=allow_internal_nav_click,
                                hover_click_words=hover_click_words,
                                widget_action_words=widget_action_words,
                                visited_keys=visited_keys, visited_families=visited_families,
                                recent_interactions=recent_interactions,
                                round_index=round_index, scroll_y=float(scroll_y),
                                allowed_url=site_url,
                            )
                            followup_total += seq_count

                # ════════════════════════════════════════════════════════
                # HOVER FALLBACK: DOM уже определил, что у элемента есть
                # CSS transitions/animations — доверяем и всегда делаем sweep.
                # page_state_changed() не ловит чисто CSS :hover эффекты.
                # ════════════════════════════════════════════════════════
                if not click_changed and (is_surface_hover or is_hover_text):
                    # Лёгкий свайп: курсор плавно проходит через элемент
                    # (1 движение — из точки входа через центр к краю), без крестов.
                    swipe_offset_x = min(max(target_width * 0.22, 18.0), viewport_width * 0.15)
                    swipe_start = (clamp(tx - swipe_offset_x, 2, viewport_width - 2), ty)
                    swipe_end = (clamp(tx + swipe_offset_x, 2, viewport_width - 2), ty)

                    try:
                        cursor_pos = await move_mouse_human_like(
                            page=page, start=cursor_pos, end=swipe_start,
                            viewport_width=viewport_width, viewport_height=viewport_height,
                            duration_ms=random.randint(search_move_min, search_move_max),
                        )
                        await page.wait_for_timeout(random.randint(30, 80))
                        cursor_pos = await move_mouse_human_like(
                            page=page, start=cursor_pos, end=swipe_end,
                            viewport_width=viewport_width, viewport_height=viewport_height,
                            duration_ms=random.randint(180, 360),
                        )
                        await page.wait_for_timeout(random.randint(micro_hover_min, micro_hover_max))
                    except Exception as exc:
                        if _is_nav_error(exc):
                            await _recover_after_nav(page)
                            visited_keys.add(target_key)
                            visited_families.add(target_family)
                            hovered_count += 1
                            interacted_this_round = True
                            continue

                    # Проверяем, был ли видимый эффект (текст/DOM изменение)
                    try:
                        after_hover_snapshot = await get_page_activity_snapshot(page)
                        hover_changed = page_state_changed(before_snapshot, after_hover_snapshot)
                    except Exception as exc:
                        if _is_nav_error(exc):
                            await _recover_after_nav(page)
                        hover_changed = False

                    if hover_changed:
                        await page.wait_for_timeout(random.randint(200, 450))

                # ── Финализация: регистрируем цель как обработанную ──
                visited_keys.add(target_key)
                visited_families.add(target_family)
                hovered_count += 1
                interacted_this_round = True
                no_progress_rounds = 0
                last_progress_at = time.monotonic()
                recent_interactions.append((tx, ty, target_abs_y, round_index))
                if len(recent_interactions) > 20:
                    recent_interactions.pop(0)
                # Запоминаем hover-зону чтобы не ходить по дочерним элементам
                # того же визуального блока.
                if (is_surface_hover or is_hover_text) and target_width > 20 and target_height > 10:
                    hovered_zones.append((
                        float(target.get("x", tx)) - target_width * 0.5,
                        target_abs_y - target_height * 0.5,
                        float(target.get("x", tx)) + target_width * 0.5,
                        target_abs_y + target_height * 0.5,
                    ))

        if round_index % 4 == 0:
            try:
                cursor_pos, _ = await try_close_overlay(page, cursor_pos, viewport_width, viewport_height)
            except Exception:
                pass
            try:
                await try_close_chat_widgets(page)
            except Exception:
                pass

        # ── Проверка лимита секции (10 с на один экран) ──
        section_elapsed_ms = (time.monotonic() - section_entered_at) * 1000
        section_scrolled_far = (scroll_y - section_scroll_y_start) >= viewport_height * 0.7
        if section_scrolled_far:
            section_scroll_y_start = scroll_y
            section_entered_at = time.monotonic()
            section_elapsed_ms = 0
        force_scroll_section_timeout = section_elapsed_ms >= section_max_ms

        should_scroll_now = True
        if force_scroll_section_timeout:
            # Секция просрочена — принудительно скроллим дальше
            should_scroll_now = True
            rounds_since_scroll = 0
        elif strict_interactions_per_scroll > 1 and interacted_this_round:
            rounds_since_scroll += 1
            if rounds_since_scroll < strict_interactions_per_scroll:
                should_scroll_now = False
            else:
                rounds_since_scroll = 0
        else:
            rounds_since_scroll = 0

        # ── Не скроллим, если в текущем viewport остались непосещённые hover-цели ──
        if should_scroll_now and not force_scroll_section_timeout and analysis_targets:
            viewport_top_check = scroll_y + int(viewport_height * 0.18)
            viewport_bottom_check = scroll_y + int(viewport_height * 0.82)
            remaining_hovers = [
                item for item in analysis_targets
                if (bool(item.get("isSurfaceHover", False)) or bool(item.get("isHoverText", False)))
                and str(item.get("key", "")) not in visited_keys
                and strict_target_family_key(item) not in visited_families
                and viewport_top_check <= int(float(item.get("absY", 0))) <= viewport_bottom_check
            ]
            if remaining_hovers:
                should_scroll_now = False

        if should_scroll_now:
            await perform_smooth_scroll(
                page=page,
                viewport_height=viewport_height,
                scroll_speed_factor=scroll_speed_factor,
                scroll_pause_min_ms=scroll_pause_min_ms,
                scroll_pause_max_ms=scroll_pause_max_ms,
            )
        else:
            await page.wait_for_timeout(random.randint(max(20, int(scroll_pause_min_ms * 0.5)), max(50, int(scroll_pause_max_ms * 0.6))))

        try:
            after_metrics = await get_scroll_metrics(page)
            current_scroll_raw = int(after_metrics.get("scrollY", scroll_y))
            if bottom_debug and last_scroll_y >= 0 and current_scroll_raw + 8 < last_scroll_y:
                logger.info(
                    "🧭 Scroll rebound detected (post-scroll): "
                    f"raw={current_scroll_raw}, last={last_scroll_y}. Игнорируем откат"
                )
            current_scroll_y = max(current_scroll_raw, max(last_scroll_y, scroll_y))
            max_scroll_y = int(after_metrics.get("maxScroll", 0))
            at_bottom = bool(after_metrics.get("atBottom", at_bottom_now))
        except Exception as exc:
            if _is_nav_error(exc):
                await _recover_after_nav(page)
                current_scroll_y = max(last_scroll_y, scroll_y)
                max_scroll_y = 0
                at_bottom = False
            else:
                raise

        scroll_advanced = current_scroll_y > (last_scroll_y + 6)
        if scroll_advanced:
            no_progress_rounds = 0
            last_progress_at = time.monotonic()
        elif not interacted_this_round:
            no_progress_rounds += 1

        stall_elapsed_ms = (time.monotonic() - last_progress_at) * 1000
        if not at_bottom and (no_progress_rounds >= 6 or stall_elapsed_ms >= stall_timeout_ms):
            logger.info(
                "🧭 Smart cursor: anti-stall форсирует выход из локального залипания "
                f"(rounds={no_progress_rounds}, elapsed={int(stall_elapsed_ms)}ms)"
            )
            await force_scroll_progress(page, viewport_height)
            interaction_pause_rounds = max(interaction_pause_rounds, 2)
            no_progress_rounds = 0
            last_progress_at = time.monotonic()

        if round_index % 12 == 0:
            logger.info(
                f"🧭 Strict progress: scrollY={current_scroll_y}, maxScroll={max_scroll_y}, "
                f"stagnant={stagnant_rounds}, hovered={hovered_count}"
            )

        if should_scroll_now:
            if current_scroll_y <= last_scroll_y + 3:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0

            if stagnant_rounds >= 3:
                await force_scroll_progress(page, viewport_height)
                stagnant_rounds = 0

        last_scroll_y = max(last_scroll_y, current_scroll_y)
        analysis_max_abs_y = max(
            (
                int(float(item.get("absY", 0.0)))
                for item in analysis_targets
                if float(item.get("absY", 0.0)) > 1.0
            ),
            default=0,
        )

        bottom_confirmed = False
        bottom_reason = "atBottom=false"
        if at_bottom:
            if max_scroll_y > 12:
                bottom_confirmed = current_scroll_y >= (max_scroll_y - 8)
                bottom_reason = f"window distance={max_scroll_y - current_scroll_y}"
            elif analysis_max_abs_y > 0:
                bottom_confirmed = analysis_max_abs_y <= (current_scroll_y + int(viewport_height * 0.94))
                bottom_reason = f"analysis tail={analysis_max_abs_y - (current_scroll_y + int(viewport_height * 0.94))}"
            else:
                bottom_confirmed = round_index >= max(6, bottom_stable_rounds_required * 2) and stagnant_rounds >= 1
                bottom_reason = (
                    "fallback "
                    f"guard_round={round_index >= max(6, bottom_stable_rounds_required * 2)} "
                    f"guard_stagnant={stagnant_rounds >= 1}"
                )
        elif max_scroll_y > 12:
            bottom_reason = f"window distance={max_scroll_y - current_scroll_y}"
        elif analysis_max_abs_y > 0:
            bottom_reason = f"analysis tail={analysis_max_abs_y - (current_scroll_y + int(viewport_height * 0.94))}"

        if bottom_debug and (
            round_index % 8 == 0
            or (at_bottom and not bottom_confirmed)
            or bottom_confirmed
        ):
            logger.info(
                "🧭 Bottom check: "
                f"atBottom={at_bottom}, confirmed={bottom_confirmed}, reason={bottom_reason}, "
                f"scrollY={current_scroll_y}, maxScroll={max_scroll_y}, stable={bottom_stable_rounds}"
            )

        if bottom_confirmed:
            bottom_stable_rounds += 1
        else:
            bottom_stable_rounds = 0

        if bottom_stable_rounds >= bottom_stable_rounds_required:
            elapsed_since_start_ms = (time.monotonic() - started_at) * 1000
            if elapsed_since_start_ms < min_duration_ms:
                # Слишком рано — страница ложно сообщает «дно». Продолжаем.
                logger.info(
                    f"🧭 Smart cursor: дно обнаружено, но прошло только "
                    f"{int(elapsed_since_start_ms)}мс из минимума {int(min_duration_ms)}мс — игнорируем"
                )
                bottom_stable_rounds = 0
                stagnant_rounds = 0
                continue

            # Дополнительная проверка: сравниваем scrollY + viewportHeight с реальной
            # высотой документа. Если мы НЕ достигли конца документа — отменяем.
            try:
                _doc_height = await page.evaluate(
                    "Math.max(document.body.scrollHeight || 0, document.documentElement.scrollHeight || 0)"
                )
                _visible_end = current_scroll_y + viewport_height
                _gap = int(_doc_height) - _visible_end
                if _gap > viewport_height * 0.15:
                    logger.info(
                        f"🧭 Smart cursor: atBottom=true, но до конца документа ещё "
                        f"{_gap}px (docH={int(_doc_height)}, visEnd={_visible_end}) — продолжаем"
                    )
                    bottom_stable_rounds = 0
                    stagnant_rounds = 0
                    # Принудительно скроллим вниз
                    await force_scroll_progress(page, viewport_height)
                    continue
            except Exception as _dh_exc:
                if _is_nav_error(_dh_exc):
                    await _recover_after_nav(page)
                    continue

            # Пробуем скроллить ещё раз (без 5с ожидания).
            _pre_check_scroll = current_scroll_y
            try:
                await force_scroll_progress(page, viewport_height)
                await page.wait_for_timeout(random.randint(400, 800))
                _recheck_metrics = await get_scroll_metrics(page)
                _recheck_scroll = int(_recheck_metrics.get("scrollY", _pre_check_scroll))
            except Exception:
                _recheck_scroll = _pre_check_scroll
            if _recheck_scroll > _pre_check_scroll + 10:
                # Контент подгрузился (lazy load) — продолжаем
                logger.info(f"🧭 Smart cursor: контент подгрузился (scroll {_pre_check_scroll} → {_recheck_scroll}), продолжаем")
                bottom_stable_rounds = 0
                last_scroll_y = _recheck_scroll
                scroll_y = _recheck_scroll
                last_progress_at = time.monotonic()
            else:
                # Реально дно — завершаем
                logger.info("🧭 Smart cursor: дно подтверждено (docHeight совпадает, скролл невозможен) — завершаем")
                reached_bottom = True
                break

    if require_bottom and not reached_bottom:
        elapsed_ms = (time.monotonic() - started_at) * 1000
        remaining_ms = max(0, int(hard_budget_ms - elapsed_ms))
        finish_budget_ms = min(max(4000, int(scroll_finish_timeout_ms)), remaining_ms)
        if finish_budget_ms <= 0:
            # Даже при достижении hard-timeout даем короткий финальный шанс дойти до низа.
            finish_budget_ms = max(8000, min(30000, int(scroll_finish_timeout_ms)))
        if finish_budget_ms > 0:
            try:
                reached_bottom = await force_scroll_to_page_end(
                    page=page,
                    viewport_height=viewport_height,
                    scroll_speed_factor=scroll_speed_factor,
                    scroll_pause_min_ms=scroll_pause_min_ms,
                    scroll_pause_max_ms=scroll_pause_max_ms,
                    finish_timeout_ms=finish_budget_ms,
                    bottom_debug=bottom_debug,
                )
            except Exception as exc:
                if _is_nav_error(exc):
                    await _recover_after_nav(page)
                else:
                    logger.warning(f"⚠️ Smart cursor: ошибка финального доскролла в STRICT режиме: {exc}")

    return cursor_pos, hovered_count, reached_bottom


async def force_scroll_to_page_end(
    page: Any,
    viewport_height: int,
    scroll_speed_factor: float,
    scroll_pause_min_ms: int,
    scroll_pause_max_ms: int,
    finish_timeout_ms: int,
    bottom_debug: bool = False,
) -> bool:
    """Финальный проход: агрессивно доскролливает страницу до конца, если основной цикл не успел."""
    if finish_timeout_ms <= 0:
        return False

    start = time.monotonic()
    stable_rounds = 0
    last_scroll = -1
    round_index = 0

    while (time.monotonic() - start) * 1000 < finish_timeout_ms:
        round_index += 1
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
            max_scroll = int(metrics.get("maxScroll", 0))
            at_bottom = bool(metrics.get("atBottom", False))
        except Exception:
            current_scroll = last_scroll
            max_scroll = 0
            at_bottom = False

        if current_scroll <= last_scroll + 2:
            await force_scroll_progress(page, viewport_height)
            try:
                await page.keyboard.press("End")
                await page.wait_for_timeout(random.randint(60, 120))
            except Exception:
                pass

        bottom_confirmed = False
        bottom_reason = "atBottom=false"
        if at_bottom:
            if max_scroll > 12:
                bottom_confirmed = current_scroll >= (max_scroll - 8)
                bottom_reason = f"window distance={max_scroll - current_scroll}"
            else:
                bottom_confirmed = round_index >= 8
                bottom_reason = f"fallback rounds={round_index}"
        elif max_scroll > 12:
            bottom_reason = f"window distance={max_scroll - current_scroll}"

        if bottom_debug and (
            round_index % 6 == 0
            or (at_bottom and not bottom_confirmed)
            or bottom_confirmed
        ):
            logger.info(
                "🧭 Force-bottom check: "
                f"atBottom={at_bottom}, confirmed={bottom_confirmed}, reason={bottom_reason}, "
                f"scrollY={current_scroll}, maxScroll={max_scroll}, stable={stable_rounds}"
            )

        if bottom_confirmed:
            stable_rounds += 1
        else:
            stable_rounds = 0

        last_scroll = max(last_scroll, current_scroll)
        if stable_rounds >= 3:
            return True

    return False


async def collect_header_nav_targets(
    page: Any,
    viewport_width: int,
    viewport_height: int,
    allow_internal_nav_click: bool,
    allowed_url: str,
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

                const visibleLeft = Math.max(0, rect.left);
                const visibleTop = Math.max(0, rect.top);
                const visibleRight = Math.min(viewportWidth, rect.right);
                const visibleBottom = Math.min(viewportHeight, rect.bottom);
                const visibleWidth = Math.max(0, visibleRight - visibleLeft);
                const visibleHeight = Math.max(0, visibleBottom - visibleTop);
                const visibleArea = visibleWidth * visibleHeight;
                const totalArea = Math.max(1, rect.width * rect.height);
                const visibleRatio = visibleArea / totalArea;
                if (visibleRatio < 0.30) continue;

                const cx = rect.left + rect.width / 2;
                const cy = rect.top + rect.height / 2;
                if (!Number.isFinite(cx) || !Number.isFinite(cy)) continue;
                if (cy > viewportHeight * 0.34 || cx < 0 || cx > viewportWidth) continue;

                const samplePoints = [
                    [cx, cy],
                    [rect.left + rect.width * 0.24, rect.top + rect.height * 0.5],
                    [rect.left + rect.width * 0.76, rect.top + rect.height * 0.5],
                ];
                let sampleCount = 0;
                let clearCount = 0;
                for (const [px, py] of samplePoints) {
                    if (px < 0 || py < 0 || px > viewportWidth || py > viewportHeight) continue;
                    sampleCount += 1;
                    const topNode = document.elementFromPoint(px, py);
                    if (
                        topNode
                        && topNode instanceof Element
                        && (el === topNode || el.contains(topNode) || topNode.contains(el))
                    ) {
                        clearCount += 1;
                    }
                }
                const visibilityClarity = sampleCount > 0 ? (clearCount / sampleCount) : 0;
                if (visibilityClarity < 0.45) continue;

                const text = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim().replace(/\\s+/g, ' ').slice(0, 64);
                let href = el instanceof HTMLAnchorElement ? (el.getAttribute('href') || '') : '';
                if (!href) { let p = el.parentElement; for (let d = 0; p && d < 4; d++, p = p.parentElement) { if (p instanceof HTMLAnchorElement) { href = p.getAttribute('href') || ''; break; } } }
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
                    visibleRatio,
                    visibilityClarity,
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
        if is_probable_top_nav_target(item, viewport_height)
        and is_safe_nav_tab_target(item, current_url, allowed_url=allowed_url)
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
    allowed_url: str,
    visited_nav_keys: Set[str],
) -> List[Dict[str, Any]]:
    """Собирает кандидатов верхней навигации и сортирует слева направо."""
    try:
        header_nav = await collect_header_nav_targets(
            page,
            viewport_width,
            viewport_height,
            allow_internal_nav_click,
            allowed_url,
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
        and is_safe_nav_tab_target(item, current_url, allowed_url=allowed_url)
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
    allowed_url: str,
    visited_nav_keys: Set[str],
    max_nav_tabs_to_visit: int,
    per_tab_scroll_timeout_ms: int,
    scroll_speed_factor: float,
    scroll_pause_min_ms: int,
    scroll_pause_max_ms: int,
    hover_min_ms: int = 220,
    hover_max_ms: int = 760,
    inpage_click_enabled: bool = True,
    inpage_click_probability: float = 0.18,
    bottom_stable_rounds_required: int = 4,
    scroll_finish_timeout_ms: int = 45000,
    hover_visible_ratio: float = 0.60,
    click_visible_ratio: float = 0.46,
    min_visibility_clarity: float = 0.42,
    click_sequence_max_steps: int = 2,
    click_sequence_radius_factor: float = 0.30,
    strict_interactions_per_scroll: int = 2,
    stall_timeout_ms: int = 10000,
    nav_budget_ms: int = 90000,
) -> Tuple[Tuple[float, float], int]:
    """Проходит по вкладкам верхней навигации последовательно с hover-эффектами на каждой странице."""
    visited_count = 0
    visited_nav_families: Set[str] = set()
    if max_nav_tabs_to_visit <= 0:
        return cursor_pos, visited_count

    nav_start_at = time.monotonic()
    original_url = str(page.url or allowed_url)

    for _ in range(max_nav_tabs_to_visit):
        # ── Глобальная проверка бюджета навигации ──
        nav_elapsed_ms = (time.monotonic() - nav_start_at) * 1000
        if nav_elapsed_ms >= nav_budget_ms:
            logger.info(f"🧭 Smart cursor: бюджет навигации исчерпан ({int(nav_elapsed_ms)}ms / {nav_budget_ms}ms), завершаем")
            break

        await ensure_page_within_allowed_site(
            page,
            allowed_url,
            fallback_url=original_url,
            timeout=15000,
        )
        # ── Вся итерация обёрнута для отказоустойчивости ──
        try:
            nav_targets = await collect_top_nav_targets(
                page,
                viewport_width,
                viewport_height,
                allow_internal_nav_click,
                allowed_url,
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

        target: Optional[Dict[str, Any]] = None
        for candidate in nav_targets:
            if nav_family_key(candidate) in visited_nav_families:
                continue
            target = candidate
            break

        if target is None:
            break

        target_family = nav_family_key(target)

        # Дополнительная проверка: self-link мог просочиться из-за изменения URL
        current_url = str(page.url or "")
        if is_nav_tab_self_link(target, current_url):
            visited_nav_keys.add(nav_signature(target))
            visited_nav_families.add(target_family)
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
                visited_nav_families.add(target_family)
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
                visited_nav_families.add(target_family)
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
        visited_nav_families.add(target_family)
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
        try:
            await try_close_chat_widgets(page)
        except Exception:
            pass

        if changed:
            # Ограничиваем per-tab бюджет остатком nav_budget_ms
            nav_remaining_ms = max(0, int(nav_budget_ms - (time.monotonic() - nav_start_at) * 1000))
            effective_tab_budget = min(per_tab_scroll_timeout_ms, nav_remaining_ms)
            if effective_tab_budget < 1500:
                logger.info("🧭 Smart cursor: бюджет навигации исчерпан внутри вкладки, завершаем")
                break
            try:
                cursor_pos, _tab_hovered, _tab_bottom = await run_strict_top_to_bottom_pass(
                    page=page,
                    cursor_pos=cursor_pos,
                    viewport_width=viewport_width,
                    viewport_height=viewport_height,
                    total_time_ms=effective_tab_budget,
                    max_targets=0,
                    hover_min_ms=hover_min_ms,
                    hover_max_ms=hover_max_ms,
                    bottom_stable_rounds_required=bottom_stable_rounds_required,
                    scroll_speed_factor=scroll_speed_factor,
                    scroll_pause_min_ms=scroll_pause_min_ms,
                    scroll_pause_max_ms=scroll_pause_max_ms,
                    inpage_click_enabled=inpage_click_enabled,
                    inpage_click_probability=inpage_click_probability,
                    scroll_finish_timeout_ms=scroll_finish_timeout_ms,
                    require_bottom=True,
                    require_bottom_max_ms=effective_tab_budget,
                    strict_allow_clicks=True,
                    bottom_debug=False,
                    hover_visible_ratio=hover_visible_ratio,
                    click_visible_ratio=click_visible_ratio,
                    min_visibility_clarity=min_visibility_clarity,
                    click_sequence_max_steps=click_sequence_max_steps,
                    click_sequence_radius_factor=click_sequence_radius_factor,
                    allow_internal_nav_click=allow_internal_nav_click,
                    site_url=allowed_url,
                    strict_interactions_per_scroll=strict_interactions_per_scroll,
                    stall_timeout_ms=stall_timeout_ms,
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
        if i == 1 or i == steps or (i % 3) == 0:
            try:
                await page.evaluate(
                    """([mx, my]) => { if (window.__vpvoaeMoveCursor) window.__vpvoaeMoveCursor(mx, my); }""",
                    [x, y],
                )
            except Exception:
                pass
        per_step_delay = max(6, int(duration_ms / steps + random.uniform(-2, 6)))
        await page.wait_for_timeout(per_step_delay)

    return end_x, end_y


async def run_smart_cursor(
    page: Any,
    site_url: str,
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
    strict_top_to_bottom_mode: bool,
    always_descend: bool,
    smart_cursor_require_bottom: bool,
    smart_cursor_require_bottom_max_ms: int,
    strict_top_to_bottom_allow_clicks: bool,
    bottom_debug: bool,
) -> int:
    """Фазовый обход сайта: сначала полный скролл, потом вкладки и интерактив."""
    start_time = time.monotonic()
    visited_keys: Set[str] = set()
    clicked_entry_keys: Set[str] = set()
    clicked_inpage_keys: Set[str] = set()
    clicked_inpage_families: Set[str] = set()
    hovered_count = 0
    recent_points: List[Tuple[float, float]] = []
    clicked_nav_keys: Set[str] = set()

    hover_visible_ratio = clamp(env_float("SMART_CURSOR_HOVER_VISIBLE_RATIO", 0.60), 0.15, 1.0)
    click_visible_ratio = clamp(env_float("SMART_CURSOR_CLICK_VISIBLE_RATIO", 0.46), 0.10, 1.0)
    min_visibility_clarity = clamp(env_float("SMART_CURSOR_MIN_VISIBILITY_CLARITY", 0.42), 0.10, 1.0)
    click_sequence_max_steps = max(1, min(env_int("SMART_CURSOR_CLICK_SEQUENCE_MAX_STEPS", 2), 4))
    click_sequence_radius_factor = clamp(env_float("SMART_CURSOR_CLICK_SEQUENCE_RADIUS_FACTOR", 0.30), 0.12, 0.55)
    strict_interactions_per_scroll = max(1, min(env_int("SMART_CURSOR_STRICT_INTERACTIONS_PER_SCROLL", 2), 4))
    strict_stall_timeout_ms = max(3500, min(env_int("SMART_CURSOR_STRICT_STALL_TIMEOUT_MS", 10000), 90000))

    cursor_pos: Tuple[float, float] = (
        viewport_width * random.uniform(0.35, 0.65),
        viewport_height * random.uniform(0.35, 0.65),
    )
    await page.mouse.move(cursor_pos[0], cursor_pos[1])
    logger.info(
        "🧭 Smart cursor visibility config: "
        f"hover_ratio={hover_visible_ratio:.2f}, click_ratio={click_visible_ratio:.2f}, "
        f"clarity={min_visibility_clarity:.2f}, click_chain_steps={click_sequence_max_steps}, "
        f"strict_interactions_per_scroll={strict_interactions_per_scroll}, "
        f"strict_stall_timeout_ms={strict_stall_timeout_ms}"
    )

    # ════════════════════════════════════════════════════════════════
    # ФАЗА 0: Клик по входным элементам (cookie, enter, welcome gate)
    # ════════════════════════════════════════════════════════════════
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

    await ensure_page_within_allowed_site(page, site_url, fallback_url=site_url, timeout=15000)

    strict_mode_active = strict_top_to_bottom_mode or always_descend
    if strict_mode_active:
        if always_descend and not strict_top_to_bottom_mode:
            logger.info("🧭 Smart cursor: включен ALWAYS_DESCEND, принудительно используем STRICT проход")
        logger.info("🧭 Smart cursor: STRICT режим (один проход сверху вниз, без переходов по страницам)")

        # ── Ожидание готовности контента: убеждаемся, что страница загрузила достаточно элементов ──
        content_wait_start = time.monotonic()
        content_wait_max_ms = 20000
        content_min_targets = 3
        while (time.monotonic() - content_wait_start) * 1000 < content_wait_max_ms:
            try:
                _probe_targets = await collect_interactive_targets(page, viewport_width, viewport_height, 50)
            except Exception:
                _probe_targets = []
            if len(_probe_targets) >= content_min_targets:
                break
            logger.info(f"🧭 Smart cursor: ожидание загрузки контента ({len(_probe_targets)} элементов)...")
            await page.wait_for_timeout(1500)

        strict_total_budget_ms = max(8000, int(total_time_ms))
        strict_main_budget_ms = strict_total_budget_ms

        cursor_pos, strict_hovered, reached_bottom = await run_strict_top_to_bottom_pass(
            page=page,
            cursor_pos=cursor_pos,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            total_time_ms=strict_main_budget_ms,
            max_targets=max_targets,
            hover_min_ms=hover_min_ms,
            hover_max_ms=hover_max_ms,
            bottom_stable_rounds_required=bottom_stable_rounds_required,
            scroll_speed_factor=scroll_speed_factor,
            scroll_pause_min_ms=scroll_pause_min_ms,
            scroll_pause_max_ms=scroll_pause_max_ms,
            inpage_click_enabled=True,
            inpage_click_probability=inpage_click_probability,
            scroll_finish_timeout_ms=scroll_finish_timeout_ms,
            require_bottom=(smart_cursor_require_bottom or always_descend),
            require_bottom_max_ms=min(smart_cursor_require_bottom_max_ms, strict_main_budget_ms),
            strict_allow_clicks=True,
            bottom_debug=bottom_debug,
            hover_visible_ratio=hover_visible_ratio,
            click_visible_ratio=click_visible_ratio,
            min_visibility_clarity=min_visibility_clarity,
            click_sequence_max_steps=click_sequence_max_steps,
            click_sequence_radius_factor=click_sequence_radius_factor,
            allow_internal_nav_click=allow_internal_nav_click,
            site_url=site_url,
            strict_interactions_per_scroll=strict_interactions_per_scroll,
            stall_timeout_ms=strict_stall_timeout_ms,
        )
        hovered_count += strict_hovered
        if reached_bottom:
            logger.info("🧭 Smart cursor: STRICT проход завершен, страница просмотрена до конца")
        else:
            logger.warning("⚠️ Smart cursor: STRICT проход завершился по hard-timeout до достижения конца страницы")

        if nav_tabs_visit_enabled and nav_tabs_max_visits > 0:
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            remaining_ms = max(0, strict_total_budget_ms - elapsed_ms)
            # Жёсткий лимит: навигация максимум 25% от общего бюджета.
            nav_budget_cap_ms = int(strict_total_budget_ms * 0.25)
            nav_budget_ms = min(remaining_ms, nav_budget_cap_ms)
            if nav_budget_ms >= 4500:
                logger.info("🧭 Smart cursor: обход вкладок навигации с hover-эффектами")
                try:
                    await page.evaluate("""() => window.scrollTo({ top: 0, left: 0, behavior: 'auto' })""")
                    await page.wait_for_timeout(random.randint(200, 420))
                except Exception:
                    pass

                per_tab_budget_ms = max(
                    1700,
                    min(nav_tab_scroll_timeout_ms, int(nav_budget_ms / max(1, nav_tabs_max_visits))),
                )
                try:
                    cursor_pos, visited_nav_count = await visit_top_navigation_tabs(
                        page=page,
                        cursor_pos=cursor_pos,
                        viewport_width=viewport_width,
                        viewport_height=viewport_height,
                        allow_internal_nav_click=allow_internal_nav_click,
                        allowed_url=site_url,
                        visited_nav_keys=clicked_nav_keys,
                        max_nav_tabs_to_visit=nav_tabs_max_visits,
                        per_tab_scroll_timeout_ms=per_tab_budget_ms,
                        scroll_speed_factor=scroll_speed_factor,
                        scroll_pause_min_ms=scroll_pause_min_ms,
                        scroll_pause_max_ms=scroll_pause_max_ms,
                        hover_min_ms=hover_min_ms,
                        hover_max_ms=hover_max_ms,
                        inpage_click_enabled=inpage_click_enabled,
                        inpage_click_probability=inpage_click_probability,
                        bottom_stable_rounds_required=bottom_stable_rounds_required,
                        scroll_finish_timeout_ms=scroll_finish_timeout_ms,
                        hover_visible_ratio=hover_visible_ratio,
                        click_visible_ratio=click_visible_ratio,
                        min_visibility_clarity=min_visibility_clarity,
                        click_sequence_max_steps=click_sequence_max_steps,
                        click_sequence_radius_factor=click_sequence_radius_factor,
                        strict_interactions_per_scroll=strict_interactions_per_scroll,
                        stall_timeout_ms=strict_stall_timeout_ms,
                        nav_budget_ms=nav_budget_ms,
                    )
                    if visited_nav_count > 0:
                        logger.info(f"🧭 Smart cursor: пройдено вкладок с hover-эффектами: {visited_nav_count}")
                except Exception as nav_err:
                    logger.warning(f"⚠️ Smart cursor: ошибка при обходе вкладок: {nav_err}")
                    await _recover_after_nav(page)
            else:
                logger.warning("⚠️ Smart cursor: не осталось бюджета для обхода вкладок")

        logger.info(f"🧭 Smart cursor: обработано интерактивных целей {hovered_count}")
        return hovered_count

    # ════════════════════════════════════════════════════════════════
    # ФАЗА 1: Полный скролл главной страницы до самого конца
    # Только скролл + лёгкий hover видимых элементов для записи.
    # Никаких кликов по вкладкам или навигации.
    # ════════════════════════════════════════════════════════════════
    logger.info("🧭 Smart cursor: ФАЗА 1 — скролл главной страницы до конца")
    main_page_scrolled = False

    if scroll_to_end:
        # Отводим щедрый бюджет: до 55% от общего времени на скролл главной
        phase1_budget_ms = max(10000, int(total_time_ms * 0.55))
        phase1_start = time.monotonic()
        last_scroll_y = -1
        stagnant_scroll_rounds = 0
        bottom_stable_rounds = 0
        phase1_round = 0

        while (time.monotonic() - phase1_start) * 1000 < phase1_budget_ms:
            phase1_round += 1

            # Закрываем модалки, если появляются
            if phase1_round % 3 == 0:
                try:
                    cursor_pos, _ = await try_close_overlay(page, cursor_pos, viewport_width, viewport_height)
                except Exception:
                    pass

            # Повторная проверка entry-кнопок (могли появиться после скролла)
            if entry_click_enabled and phase1_round % 5 == 1:
                try:
                    cursor_pos, clicked, clicked_key = await try_click_entry_element(
                        page=page, cursor_pos=cursor_pos,
                        viewport_width=viewport_width, viewport_height=viewport_height,
                        clicked_keys=clicked_entry_keys,
                    )
                    if clicked and clicked_key:
                        clicked_entry_keys.add(clicked_key)
                except Exception:
                    pass

            # Лёгкий hover по видимым элементам (для записи эффектов), но БЕЗ кликов
            try:
                targets = await collect_interactive_targets(page, viewport_width, viewport_height, 20)
            except Exception:
                targets = []
            hover_candidates = [
                item for item in targets
                if str(item.get("key", "")) not in visited_keys
                and not is_probable_top_nav_target(item, viewport_height)
                and is_target_clearly_visible(
                    item,
                    viewport_width,
                    viewport_height,
                    min_visible_ratio=hover_visible_ratio,
                    min_visibility_clarity=min_visibility_clarity,
                    resolved_y=_to_float(item.get("y"), viewport_height * 0.5),
                )
            ]
            if hover_candidates and random.random() < 0.55:
                surface_candidates = [item for item in hover_candidates if bool(item.get("isSurfaceHover", False))]
                if surface_candidates and random.random() < 0.74:
                    pick = random.choice(surface_candidates[: min(5, len(surface_candidates))])
                else:
                    pick = random.choice(hover_candidates[:5])
                tx = float(pick["x"])
                ty = float(pick["y"])
                target_width = float(pick.get("width", 0.0))
                target_height = float(pick.get("height", 0.0))
                is_surface_hover = bool(pick.get("isSurfaceHover", False))
                try:
                    if is_surface_hover and (target_width >= 120 or target_height >= 90):
                        span_x = min(max(target_width * 0.22, 22.0), viewport_width * 0.22)
                        span_y = min(max(target_height * 0.17, 18.0), viewport_height * 0.18)
                        path_points = [
                            (clamp(tx - span_x, 2, viewport_width - 2), ty),
                            (tx, clamp(ty - span_y, 2, viewport_height - 2)),
                            (clamp(tx + span_x, 2, viewport_width - 2), ty),
                            (tx, clamp(ty + span_y, 2, viewport_height - 2)),
                            (tx, ty),
                        ]
                        for point in path_points:
                            cursor_pos = await move_mouse_human_like(
                                page, cursor_pos, point,
                                viewport_width, viewport_height, random.randint(60, 160),
                            )
                            await page.wait_for_timeout(random.randint(max(35, int(hover_min_ms * 0.25)), max(70, int(hover_max_ms * 0.40))))
                    else:
                        cursor_pos = await move_mouse_human_like(
                            page, cursor_pos, (tx, ty),
                            viewport_width, viewport_height, random.randint(80, 250),
                        )
                        await page.wait_for_timeout(random.randint(int(hover_min_ms * 0.6), int(hover_max_ms * 0.7)))
                    visited_keys.add(str(pick.get("key", "")))
                    hovered_count += 1
                except Exception:
                    pass

            # Основное действие фазы: скролл вниз
            await perform_smooth_scroll(
                page, viewport_height, scroll_speed_factor,
                scroll_pause_min_ms, scroll_pause_max_ms,
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

            if stagnant_scroll_rounds >= 3:
                try:
                    await page.keyboard.press("PageDown")
                    await page.wait_for_timeout(random.randint(30, 80))
                except Exception:
                    pass
                try:
                    await page.keyboard.press("End")
                    await page.wait_for_timeout(random.randint(60, 120))
                except Exception:
                    pass

            last_scroll_y = max(last_scroll_y, current_scroll_y)
            if at_bottom:
                bottom_stable_rounds += 1
            else:
                bottom_stable_rounds = 0

            if bottom_stable_rounds >= bottom_stable_rounds_required:
                main_page_scrolled = True
                logger.info("🧭 Smart cursor: главная страница прокручена до конца")
                break

        # Финальный рывок если ещё не дошли
        if not main_page_scrolled:
            try:
                reached = await force_scroll_to_page_end(
                    page, viewport_height, scroll_speed_factor,
                    scroll_pause_min_ms, scroll_pause_max_ms,
                    max(5000, int(phase1_budget_ms * 0.3)),
                )
                if reached:
                    main_page_scrolled = True
                    logger.info("🧭 Smart cursor: главная страница прокручена до конца (финальный рывок)")
            except Exception as exc:
                if _is_nav_error(exc):
                    await _recover_after_nav(page)
                else:
                    logger.warning(f"⚠️ Smart cursor: ошибка при финальном скролле фазы 1: {exc}")

        if not main_page_scrolled:
            logger.warning("⚠️ Smart cursor: не удалось прокрутить главную до конца, переходим к следующей фазе")

        # Возврат наверх
        try:
            await page.evaluate("""() => window.scrollTo({ top: 0, left: 0, behavior: 'auto' })""")
            await page.wait_for_timeout(random.randint(300, 600))
        except Exception:
            pass

    # ════════════════════════════════════════════════════════════════
    # ФАЗА 2: Обход вкладок навигации (только после полного скролла)
    # Для каждой вкладки: открыть → проскроллить до конца → вернуться
    # ════════════════════════════════════════════════════════════════
    if nav_tabs_visit_enabled:
        remaining_ms = total_time_ms - (time.monotonic() - start_time) * 1000
        if remaining_ms > 8000:
            logger.info("🧭 Smart cursor: ФАЗА 2 — обход вкладок навигации")
            try:
                cursor_pos, visited_nav_count = await visit_top_navigation_tabs(
                    page, cursor_pos,
                    viewport_width, viewport_height,
                    allow_internal_nav_click, site_url, clicked_nav_keys,
                    nav_tabs_max_visits, nav_tab_scroll_timeout_ms,
                    scroll_speed_factor, scroll_pause_min_ms, scroll_pause_max_ms,
                    hover_min_ms=hover_min_ms,
                    hover_max_ms=hover_max_ms,
                    inpage_click_enabled=inpage_click_enabled,
                    inpage_click_probability=inpage_click_probability,
                    bottom_stable_rounds_required=bottom_stable_rounds_required,
                    scroll_finish_timeout_ms=scroll_finish_timeout_ms,
                    hover_visible_ratio=hover_visible_ratio,
                    click_visible_ratio=click_visible_ratio,
                    min_visibility_clarity=min_visibility_clarity,
                    click_sequence_max_steps=click_sequence_max_steps,
                    click_sequence_radius_factor=click_sequence_radius_factor,
                    strict_interactions_per_scroll=strict_interactions_per_scroll,
                    stall_timeout_ms=strict_stall_timeout_ms,
                )
                if visited_nav_count > 0:
                    logger.info(f"🧭 Smart cursor: пройдено вкладок {visited_nav_count}")
            except Exception as nav_err:
                logger.warning(f"⚠️ Smart cursor: ошибка при обходе вкладок: {nav_err}")
                await _recover_after_nav(page)

    # ════════════════════════════════════════════════════════════════
    # ФАЗА 3: Интерактивный обход — hover + клики по оставшимся элементам
    # Работаем с оставшимся бюджетом времени
    # ════════════════════════════════════════════════════════════════
    remaining_ms = total_time_ms - (time.monotonic() - start_time) * 1000
    if remaining_ms > 3000:
        logger.info("🧭 Smart cursor: ФАЗА 3 — интерактивный обход элементов")

        # Возврат наверх перед интерактивным обходом
        try:
            await page.evaluate("""() => window.scrollTo({ top: 0, left: 0, behavior: 'auto' })""")
            await page.wait_for_timeout(random.randint(200, 400))
        except Exception:
            pass

        nav_keywords = ("list", "grid", "stills", "motion", "culture", "information", "journal")
        hover_click_words = (
            "click", "tap", "press", "here", "open", "show", "reveal",
            "start", "play", "go", "next", "more", "toggle", "menu",
            "try", "view", "explore", "activate", "continue", "enter",
        )
        widget_action_words = (
            "accept", "reset", "submit", "next", "confirm", "apply",
            "calculate", "done", "save", "select", "choose", "finish",
        )
        phase3_start = time.monotonic()
        phase3_budget_ms = remaining_ms
        last_scroll_y = -1

        try:
            last_scroll_y = int((await get_scroll_metrics(page)).get("scrollY", 0))
        except Exception:
            last_scroll_y = -1

        round_index = 0
        while (time.monotonic() - phase3_start) * 1000 < phase3_budget_ms:
            round_index += 1

            await ensure_page_within_allowed_site(
                page,
                site_url,
                fallback_url=site_url,
                timeout=15000,
            )

            if round_index % 3 == 0:
                try:
                    cursor_pos, _ = await try_close_overlay(page, cursor_pos, viewport_width, viewport_height)
                except Exception:
                    pass

            scan_limit = max(40, max_targets if max_targets > 0 else 40)
            try:
                targets = await collect_interactive_targets(page, viewport_width, viewport_height, scan_limit)
            except Exception as exc:
                if _is_nav_error(exc):
                    await _recover_after_nav(page)
                targets = []

            candidates = [
                item
                for item in targets
                if str(item.get("key", "")) not in visited_keys
                and is_target_clearly_visible(
                    item,
                    viewport_width,
                    viewport_height,
                    min_visible_ratio=hover_visible_ratio,
                    min_visibility_clarity=min_visibility_clarity,
                    resolved_y=_to_float(item.get("y"), viewport_height * 0.5),
                )
            ]
            current_url = str(page.url or "")

            safe_click_candidates = [
                item for item in candidates
                if is_safe_inpage_click_target(item, current_url, allow_internal_nav_click, allowed_url=site_url)
                and is_target_clearly_visible(
                    item,
                    viewport_width,
                    viewport_height,
                    min_visible_ratio=click_visible_ratio,
                    min_visibility_clarity=min_visibility_clarity,
                    resolved_y=_to_float(item.get("y"), viewport_height * 0.5),
                )
            ]
            media_candidates = [
                item for item in safe_click_candidates
                if float(item.get("width", 0.0)) * float(item.get("height", 0.0)) >= 35000
                and float(item.get("y", viewport_height)) > viewport_height * 0.12
                and not is_probable_top_nav_target(item, viewport_height)
            ]

            target: Optional[Dict[str, Any]] = None
            if inpage_click_enabled and media_candidates and random.random() < 0.60:
                target = random.choice(media_candidates[: min(5, len(media_candidates))])
            elif candidates and (max_targets <= 0 or hovered_count < max_targets):
                surface_hover_candidates = [item for item in candidates if bool(item.get("isSurfaceHover", False))]
                if surface_hover_candidates and random.random() < 0.62:
                    top_pool = sorted(surface_hover_candidates, key=target_sort_score, reverse=True)[:8]
                else:
                    top_pool = sorted(candidates, key=target_sort_score, reverse=True)[:8]
                target = random.choice(top_pool[:3] if len(top_pool) >= 3 else top_pool)

            if target is None and candidates:
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
                target_width = float(target.get("width", 0.0))
                target_height = float(target.get("height", 0.0))
                is_surface_hover = bool(target.get("isSurfaceHover", False))
                try:
                    if is_surface_hover and (target_width >= 120 or target_height >= 90):
                        # Лёгкий свайп вместо крестообразного прохода
                        swipe_ox = min(max(target_width * 0.22, 18.0), viewport_width * 0.15)
                        sp_start = (clamp(tx - swipe_ox, 1, viewport_width - 1), ty)
                        sp_end = (clamp(tx + swipe_ox, 1, viewport_width - 1), ty)
                        cursor_pos = await move_mouse_human_like(
                            page, cursor_pos, sp_start,
                            viewport_width, viewport_height, random.randint(60, 150),
                        )
                        await page.wait_for_timeout(random.randint(30, 70))
                        cursor_pos = await move_mouse_human_like(
                            page, cursor_pos, sp_end,
                            viewport_width, viewport_height, random.randint(150, 320),
                        )
                        await page.wait_for_timeout(random.randint(max(40, int(hover_min_ms * 0.30)), max(90, int(hover_max_ms * 0.50))))
                    else:
                        cursor_pos = await move_mouse_human_like(
                            page, cursor_pos, (tx, ty),
                            viewport_width, viewport_height, random.randint(260, 860),
                        )
                        await page.wait_for_timeout(random.randint(40, 120))

                        jitter_rx = max(2.0, min(target_width * 0.12, 14.0))
                        jitter_ry = max(2.0, min(target_height * 0.12, 12.0))
                        jx = clamp(tx + random.uniform(-jitter_rx, jitter_rx), 1, viewport_width - 1)
                        jy = clamp(ty + random.uniform(-jitter_ry, jitter_ry), 1, viewport_height - 1)
                        await page.mouse.move(jx, jy)
                        await page.wait_for_timeout(random.randint(24, 80))
                        await page.wait_for_timeout(random.randint(hover_min_ms, hover_max_ms))
                except Exception as exc:
                    if _is_nav_error(exc):
                        await _recover_after_nav(page)
                        continue
                    # Non-critical mouse error — continue

                target_key = str(target.get("key", ""))
                target_family = strict_target_family_key(target)
                target_text = str(target.get("text", ""))
                target_text_low = target_text.strip().lower()
                target_href = str(target.get("href", "")).strip().lower()
                target_tag = str(target.get("tag", "")).strip().lower()
                is_hover_text = bool(target.get("isHoverText", False))

                action_hint = has_keyword(target_text_low, hover_click_words) or has_keyword(target_text_low, widget_action_words)
                is_local_trigger = (
                    target_tag in {"button", "summary"}
                    or (
                        not target_href
                        and (action_hint or (target_width * target_height <= 28000 and not is_surface_hover))
                    )
                )

                should_click = (
                    inpage_click_enabled
                    and target_key not in clicked_inpage_keys
                    and target_family not in clicked_inpage_families
                    and is_safe_inpage_click_target(target, current_url, allow_internal_nav_click, allowed_url=site_url)
                    and is_target_clearly_visible(
                        target,
                        viewport_width,
                        viewport_height,
                        min_visible_ratio=click_visible_ratio,
                        min_visibility_clarity=min_visibility_clarity,
                        resolved_y=ty,
                    )
                )

                click_probability = inpage_click_probability
                if has_keyword(str(target.get("text", "")), nav_keywords):
                    click_probability = max(click_probability, 0.88)
                if is_probable_top_nav_target(target, viewport_height):
                    click_probability = max(click_probability, 0.92)
                if float(target.get("width", 0.0)) * float(target.get("height", 0.0)) >= 35000:
                    click_probability = min(max(click_probability, 0.22), 0.42)

                if has_keyword(target_text, widget_action_words):
                    click_probability = max(click_probability, 0.88)
                if action_hint:
                    click_probability = 1.0
                if is_local_trigger:
                    click_probability = max(click_probability, 0.84)
                if is_hover_text and is_local_trigger:
                    click_probability = max(click_probability, 0.94)
                if is_surface_hover and is_local_trigger:
                    click_probability = max(click_probability, 0.56)
                if target_tag in {"button", "summary"} and not target_href:
                    click_probability = max(click_probability, 0.90)

                hover_followup_count = 0
                if inpage_click_enabled and (is_surface_hover or is_hover_text) and click_sequence_max_steps > 0:
                    hover_followup_steps = max(2, min(click_sequence_max_steps + 2, 5))
                    cursor_pos, hover_followup_count = await perform_followup_click_sequence(
                        page=page,
                        cursor_pos=cursor_pos,
                        anchor_x=tx,
                        anchor_y=ty,
                        anchor_key=target_key,
                        anchor_family=target_family,
                        viewport_width=viewport_width,
                        viewport_height=viewport_height,
                        clicked_keys=clicked_inpage_keys,
                        clicked_families=clicked_inpage_families,
                        hover_min_ms=hover_min_ms,
                        hover_max_ms=hover_max_ms,
                        max_followup_steps=hover_followup_steps,
                        radius_factor=click_sequence_radius_factor,
                        min_visible_ratio=click_visible_ratio,
                        min_visibility_clarity=min_visibility_clarity,
                        allow_internal_nav_click=allow_internal_nav_click,
                        hover_click_words=hover_click_words,
                        widget_action_words=widget_action_words,
                        visited_keys=visited_keys,
                        allowed_url=site_url,
                    )
                    if hover_followup_count > 0:
                        logger.info(
                            "🖱️ Smart cursor: phase-3 hover раскрыл локальные клики "
                            f"(дополнительных шагов={hover_followup_count})"
                        )

                if should_click and hover_followup_count == 0 and random.random() < click_probability:
                    before_url = str(page.url or "")
                    try:
                        await page.mouse.click(tx, ty, delay=random.randint(35, 110))
                        await page.wait_for_timeout(random.randint(120, 300))
                    except Exception as exc:
                        if _is_nav_error(exc):
                            await _recover_after_nav(page)
                            continue
                    clicked_inpage_keys.add(target_key)
                    clicked_inpage_families.add(target_family)

                    if is_probable_top_nav_target(target, viewport_height):
                        clicked_nav_keys.add(nav_signature(target))

                    try:
                        cursor_pos, _ = await try_close_overlay(page, cursor_pos, viewport_width, viewport_height)
                    except Exception:
                        pass

                    after_url = str(page.url or "")
                    navigated_offsite = after_url and not is_same_site_url(after_url, site_url)
                    navigated_internally = after_url != before_url and is_navigation_like_href(after_url, before_url)
                    if navigated_offsite:
                        logger.warning("⛔ Smart cursor: phase-3 поймал внешний переход, возвращаемся назад")
                        await ensure_page_within_allowed_site(
                            page,
                            site_url,
                            fallback_url=before_url,
                            timeout=15000,
                        )
                    elif navigated_internally and not allow_internal_nav_click:
                        logger.info("🖱️ Smart cursor: обнаружен внутренний переход, откатываемся назад")
                        await restore_page_location(
                            page,
                            before_url,
                            timeout=12000,
                        )
                    elif after_url != before_url and allow_internal_nav_click:
                        logger.info("🖱️ Smart cursor: выполнен внутренний переход по интерактиву")
                    else:
                        followup_steps = max(1, min(click_sequence_max_steps + 1, 5))
                        if followup_steps > 0 and (action_hint or is_local_trigger or is_hover_text):
                            cursor_pos, followup_count = await perform_followup_click_sequence(
                                page=page,
                                cursor_pos=cursor_pos,
                                anchor_x=tx,
                                anchor_y=ty,
                                anchor_key=target_key,
                                anchor_family=target_family,
                                viewport_width=viewport_width,
                                viewport_height=viewport_height,
                                clicked_keys=clicked_inpage_keys,
                                clicked_families=clicked_inpage_families,
                                hover_min_ms=hover_min_ms,
                                hover_max_ms=hover_max_ms,
                                max_followup_steps=followup_steps,
                                radius_factor=click_sequence_radius_factor,
                                min_visible_ratio=click_visible_ratio,
                                min_visibility_clarity=min_visibility_clarity,
                                allow_internal_nav_click=allow_internal_nav_click,
                                hover_click_words=hover_click_words,
                                widget_action_words=widget_action_words,
                                visited_keys=visited_keys,
                                allowed_url=site_url,
                            )
                            if followup_count > 0:
                                logger.info(
                                    "🖱️ Smart cursor: phase-3 цепочка локальных кликов "
                                    f"(дополнительных шагов={followup_count})"
                                )

                hovered_count += 1
                visited_keys.add(target_key)
                recent_points.append((tx, ty))
                if len(recent_points) > 6:
                    recent_points.pop(0)

            # Подскролливаем между hover-ами для охвата всей страницы
            if scroll_to_end and round_index % 2 == 0:
                await perform_smooth_scroll(
                    page, viewport_height, scroll_speed_factor,
                    scroll_pause_min_ms, scroll_pause_max_ms,
                )

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
        smart_cursor_timeout = int(os.getenv('SMART_CURSOR_TIMEOUT', '360000'))
        smart_cursor_max_targets = int(os.getenv('SMART_CURSOR_MAX_TARGETS', '0'))
        hover_min_ms = int(os.getenv('SMART_CURSOR_HOVER_MIN_MS', '220'))
        hover_max_ms = int(os.getenv('SMART_CURSOR_HOVER_MAX_MS', '760'))
        entry_click_enabled = env_bool('SMART_CURSOR_ENTRY_CLICK_ENABLED', True)
        entry_click_attempts = int(os.getenv('SMART_CURSOR_ENTRY_CLICK_ATTEMPTS', '3'))
        scroll_to_end = env_bool('SMART_CURSOR_SCROLL_TO_END', True)
        bottom_stable_rounds_required = int(os.getenv('SMART_CURSOR_BOTTOM_STABLE_ROUNDS', '4'))
        scroll_speed_factor = float(os.getenv('SMART_CURSOR_SCROLL_SPEED', '1.25'))
        scroll_pause_min_ms = int(os.getenv('SMART_CURSOR_SCROLL_PAUSE_MIN_MS', '24'))
        scroll_pause_max_ms = int(os.getenv('SMART_CURSOR_SCROLL_PAUSE_MAX_MS', '70'))
        scroll_finish_timeout_ms = int(os.getenv('SMART_CURSOR_SCROLL_FINISH_TIMEOUT_MS', '45000'))
        nav_tabs_visit_enabled = env_bool('SMART_CURSOR_NAV_TABS_VISIT_ENABLED', True)
        nav_tabs_max_visits = int(os.getenv('SMART_CURSOR_NAV_TABS_MAX_VISITS', '10'))
        nav_tab_scroll_timeout_ms = int(os.getenv('SMART_CURSOR_NAV_TAB_SCROLL_TIMEOUT_MS', '17000'))
        inpage_click_enabled = env_bool('SMART_CURSOR_INPAGE_CLICK_ENABLED', True)
        inpage_click_probability = float(os.getenv('SMART_CURSOR_INPAGE_CLICK_PROBABILITY', '0.18'))
        allow_internal_nav_click = env_bool('SMART_CURSOR_ALLOW_INTERNAL_NAV_CLICK', False)
        strict_top_to_bottom_mode = env_bool('SMART_CURSOR_STRICT_TOP_TO_BOTTOM', True)
        smart_cursor_always_descend = env_bool('SMART_CURSOR_ALWAYS_DESCEND', True)
        strict_top_to_bottom_allow_clicks = env_bool('SMART_CURSOR_STRICT_ALLOW_CLICKS', True)
        smart_cursor_require_bottom = env_bool('SMART_CURSOR_REQUIRE_BOTTOM', True)
        smart_cursor_require_bottom_max_ms = int(os.getenv('SMART_CURSOR_REQUIRE_BOTTOM_MAX_MS', '900000'))
        smart_cursor_bottom_debug = env_bool('SMART_CURSOR_BOTTOM_DEBUG', False)
        screenshot_enabled = env_bool('SCREENSHOT_ENABLED', True)
        screenshot_timeout_ms = int(os.getenv('SCREENSHOT_TIMEOUT_MS', '8000'))
        browser_fullscreen = env_bool('BROWSER_FULLSCREEN', True)
        browser_app_mode = env_bool('BROWSER_APP_MODE', True)
        browser_performance_mode = env_bool('BROWSER_PERFORMANCE_MODE', True)
        visible_cursor_enabled = env_bool('VISIBLE_CURSOR_ENABLED', True)

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
        smart_cursor_timeout = max(8000, min(smart_cursor_timeout, 900000))
        smart_cursor_require_bottom_max_ms = max(30000, min(smart_cursor_require_bottom_max_ms, 900000))
        screenshot_timeout_ms = max(1000, min(screenshot_timeout_ms, 60000))
        if smart_cursor_require_bottom and smart_cursor_require_bottom_max_ms < smart_cursor_timeout:
            smart_cursor_require_bottom_max_ms = smart_cursor_timeout
        
        logger.info("🚀 Запуск VPVoAe Web Renderer")
        logger.info(f"Target URL: {target_url}")
        logger.info(f"Viewport: {viewport_width}x{viewport_height}")
        logger.info(f"Display: {os.getenv('DISPLAY', ':99')}")
        logger.info("📹 Video recording: ENABLED (запись идёт параллельно)")
        logger.info(f"🧭 Smart cursor: {'ENABLED' if smart_cursor_enabled else 'DISABLED'}")
        logger.info(f"🧭 Smart cursor strict top-to-bottom: {'ENABLED' if strict_top_to_bottom_mode else 'DISABLED'}")
        logger.info(f"🧭 Smart cursor always descend: {'ENABLED' if smart_cursor_always_descend else 'DISABLED'}")
        logger.info(f"🧭 Smart cursor strict allow clicks: {'ENABLED' if strict_top_to_bottom_allow_clicks else 'DISABLED'}")
        logger.info(f"🧭 Smart cursor require bottom: {'ENABLED' if smart_cursor_require_bottom else 'DISABLED'} (max={smart_cursor_require_bottom_max_ms}ms)")
        logger.info(f"🧭 Smart cursor bottom debug: {'ENABLED' if smart_cursor_bottom_debug else 'DISABLED'}")
        logger.info(f"📸 Screenshot: {'ENABLED' if screenshot_enabled else 'DISABLED'} (timeout={screenshot_timeout_ms}ms)")
        logger.info(f"🖥️ Browser fullscreen: {'ENABLED' if browser_fullscreen else 'DISABLED'}")
        logger.info(f"🧱 Browser app mode: {'ENABLED' if browser_app_mode else 'DISABLED'}")
        logger.info(f"⚡ Browser performance mode: {'ENABLED' if browser_performance_mode else 'DISABLED'}")
        logger.info(f"🖱️ Visible cursor overlay: {'ENABLED' if visible_cursor_enabled else 'DISABLED'}")
        
        async with async_playwright() as p:
            logger.info("🌐 Запуск браузера на виртуальном дисплее...")
            browser_args = [
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
                '--no-sandbox',
                '--disable-extensions',
                '--kiosk',
                '--start-fullscreen',
                '--start-maximized',
                '--window-position=0,0',
                f'--window-size={viewport_width},{viewport_height}',
                '--hide-crash-restore-bubble',
                '--disable-infobars',
            ]

            if browser_performance_mode:
                browser_args.extend([
                    '--ignore-gpu-blocklist',
                    '--enable-webgl',
                    '--enable-unsafe-swiftshader',
                    '--use-angle=swiftshader',
                    '--enable-gpu-rasterization',
                    '--enable-zero-copy',
                    '--disable-renderer-backgrounding',
                    '--disable-backgrounding-occluded-windows',
                    '--disable-background-timer-throttling',
                ])

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

            async def route_main_document(route, request) -> None:
                try:
                    is_navigation = bool(request.is_navigation_request()) and request.resource_type == "document"
                    frame = request.frame
                    is_top_level = getattr(frame, "parent_frame", None) is None
                    if is_navigation and is_top_level and not is_same_site_url(request.url, target_url):
                        logger.warning(f"⛔ Site guard: блокируем внешний top-level переход {request.url}")
                        await route.abort()
                        return
                except Exception:
                    pass
                await route.continue_()

            await context.route("**/*", route_main_document)

            if visible_cursor_enabled:
                await context.add_init_script(
                    """
                    (() => {
                        function ensureCursor() {
                            let el = document.getElementById('__vpvoae_visible_cursor');
                            if (!el) {
                                el = document.createElement('div');
                                el.id = '__vpvoae_visible_cursor';
                                el.setAttribute('aria-hidden', 'true');
                                el.style.position = 'fixed';
                                el.style.left = '0';
                                el.style.top = '0';
                                el.style.width = '22px';
                                el.style.height = '22px';
                                el.style.pointerEvents = 'none';
                                el.style.zIndex = '2147483647';
                                el.style.opacity = '0.98';
                                el.style.transform = 'translate3d(-100px,-100px,0)';
                                el.style.filter = 'drop-shadow(0 1px 2px rgba(0,0,0,0.55))';
                                el.innerHTML = '<svg width="22" height="22" viewBox="0 0 22 22" xmlns="http://www.w3.org/2000/svg"><path d="M2 1.5L2 16.2L6.7 12.2L9.8 19.8L12.6 18.7L9.6 11.3L16.6 11.3L2 1.5Z" fill="#ffffff" stroke="#111111" stroke-width="1.2" stroke-linejoin="round"/></svg>';
                            }

                            if (!el.isConnected) {
                                (document.documentElement || document.body).appendChild(el);
                            } else if (el.parentElement !== (document.documentElement || document.body)) {
                                el.remove();
                                (document.documentElement || document.body).appendChild(el);
                            }

                            return el;
                        }

                        function moveCursor(x, y) {
                            const el = ensureCursor();
                            const mx = Number.isFinite(x) ? Math.round(x) : 0;
                            const my = Number.isFinite(y) ? Math.round(y) : 0;
                            el.style.transform = `translate3d(${mx}px, ${my}px, 0)`;
                        }

                        window.__vpvoaeEnsureCursor = ensureCursor;
                        window.__vpvoaeMoveCursor = moveCursor;

                        const pointerHandler = (ev) => {
                            const ex = Number(ev && ev.clientX);
                            const ey = Number(ev && ev.clientY);
                            moveCursor(Number.isFinite(ex) ? ex : 0, Number.isFinite(ey) ? ey : 0);
                        };

                        window.addEventListener('mousemove', pointerHandler, { passive: true });
                        window.addEventListener('pointermove', pointerHandler, { passive: true });
                        window.addEventListener('scroll', () => ensureCursor(), { passive: true });

                        if (document.readyState === 'loading') {
                            document.addEventListener('DOMContentLoaded', () => ensureCursor(), { once: true });
                        } else {
                            ensureCursor();
                        }
                    })();
                    """
                )

            page = await context.new_page()

            if visible_cursor_enabled:
                try:
                    await page.evaluate("""() => { if (window.__vpvoaeEnsureCursor) window.__vpvoaeEnsureCursor(); }""")
                except Exception:
                    pass

            logger.info(f"📄 Открываем целевой сайт: {target_url}")
            # Сначала быстрый DOM-ready, затем короткая попытка дождаться networkidle.
            try:
                await page.goto(
                    target_url,
                    wait_until="domcontentloaded",
                    timeout=load_timeout
                )
                try:
                    await page.wait_for_load_state("networkidle", timeout=min(12000, max(2500, int(load_timeout * 0.35))))
                except Exception:
                    pass
                if visible_cursor_enabled:
                    try:
                        await page.evaluate("""() => { if (window.__vpvoaeEnsureCursor) window.__vpvoaeEnsureCursor(); }""")
                    except Exception:
                        pass
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
                allowed_site_url = str(page.url or target_url)
                await run_smart_cursor(
                    page=page,
                    site_url=allowed_site_url,
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
                    strict_top_to_bottom_mode=strict_top_to_bottom_mode,
                    always_descend=smart_cursor_always_descend,
                    smart_cursor_require_bottom=smart_cursor_require_bottom,
                    smart_cursor_require_bottom_max_ms=smart_cursor_require_bottom_max_ms,
                    strict_top_to_bottom_allow_clicks=strict_top_to_bottom_allow_clicks,
                    bottom_debug=smart_cursor_bottom_debug,
                )
            else:
                logger.info("🧭 Smart cursor пропущен по конфигурации")

            # Создаем директорию для результатов
            os.makedirs(output_path, exist_ok=True)

            if screenshot_enabled:
                logger.info("📸 Создание скриншота...")
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                screenshot_path = os.path.join(output_path, f"screenshot_{timestamp}.png")
                latest_path = os.path.join(output_path, "screenshot_latest.png")

                try:
                    await page.screenshot(
                        path=screenshot_path,
                        full_page=False,
                        omit_background=False,
                        timeout=screenshot_timeout_ms,
                    )
                    await page.screenshot(
                        path=latest_path,
                        full_page=False,
                        omit_background=False,
                        timeout=screenshot_timeout_ms,
                    )

                    file_size = os.path.getsize(screenshot_path) / (1024 * 1024)  # MB
                    logger.info(f"✅ Скриншот сохранен: {screenshot_path} ({file_size:.2f}MB)")
                    logger.info(f"✅ Latest: {latest_path}")
                except Exception as screenshot_exc:
                    logger.warning(f"⚠️ Не удалось сохранить скриншот: {screenshot_exc}")
            else:
                logger.info("📸 Скриншот пропущен по конфигурации")

            await context.close()
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
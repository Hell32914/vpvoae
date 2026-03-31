import asyncio
import os
import shutil
import sys
import logging
import math
import random
import signal
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from datetime import datetime
import subprocess
from urllib.parse import urljoin, urlparse
from playwright.async_api import async_playwright

try:
    from playwright_stealth import stealth_async as _stealth_async
    _STEALTH_AVAILABLE = True
except ImportError:
    _STEALTH_AVAILABLE = False

# Realistic modern Chrome UA on Windows 11 — masks Playwright automation fingerprint
_STEALTH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

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


def resolve_smart_cursor_mode(raw_value: Optional[str]) -> str:
    raw_mode = (raw_value or "").strip().lower()
    aliases = {
        "": "default",
        "default": "default",
        "standard": "default",
        "legacy": "default",
        "explorer": "default",
        "hover": "default",
        "scroll_only": "scroll_only",
        "scroll-only": "scroll_only",
        "scrollonly": "scroll_only",
        "descend_only": "scroll_only",
        "descend-only": "scroll_only",
        "down_only": "scroll_only",
        "down-only": "scroll_only",
        "cta_analyzer": "scroll_only",
        "cta-analyzer": "scroll_only",
        "ctaanalyzer": "scroll_only",
        "cta": "scroll_only",
        "analyzer": "scroll_only",
    }
    resolved = aliases.get(raw_mode)
    if resolved:
        return resolved
    logger.warning(f"⚠️ Неизвестный SMART_CURSOR_MODE='{raw_value}', используем default")
    return "default"


def clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(value, max_value))


def _url_page_path(url: str) -> str:
    """Возвращает нормализованный path URL без trailing-slash и фрагментов.
    Используется для сравнения «мы на той же странице».
    """
    try:
        parts = urlparse(url)
        path = (parts.path or "/").rstrip("/") or "/"
        return path
    except Exception:
        return "/"


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
        "enter", "start", "continue", "explore", "open", "begin", "launch", "proceed",
        "visit", "view", "discover", "watch", "play", "skip", "next", "close", "ok", "accept",
        "agree", "allow", "got it", "i understand", "войти", "начать", "продолж", "далее", "принять",
    )
    # Эти слова блокируют bonus чтобы auth/nav кнопки не получали +430
    auth_nav_words = (
        "signup", "sign up", "login", "log in", "register", "create account",
        "google", "linkedin", "facebook", "github", "apple",
        "get started", "free trial", "try for free",
    )

    keyword_bonus = 0.0
    # Используем has_keyword (word-token matching) а не plain substring для primary_keywords
    if has_keyword(text, primary_keywords) and not has_keyword(text, auth_nav_words):
        keyword_bonus += 430.0
    if has_keyword(href, primary_keywords) and not has_keyword(href, auth_nav_words):
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
        "continue", "open", "skip", "next", "ok", "продолж", "далее",
    )
    # Эти слова НИКОГДА не должны триггерить Phase-0 click — они навигационные CTA,
    # а не gate/overlay (cookie-wall, age-gate, welcome screen).
    purchase_words = (
        "buy", "shop", "cart", "checkout", "pricing", "price", "guide", "ebook", "course",
        "purchase", "subscribe", "plan", "membership", "donate", "book", "store",
        # Auth buttons — никогда не являются входными gate-кнопками
        "signup", "sign up", "sign-up",
        "login", "log in", "log-in",
        "register", "create account",
        "with google", "with linkedin", "with facebook", "with github", "with apple",
        "oauth", "sso", "saml",
        "free trial", "get started", "try for free", "try free", "start free",
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

    before_url = str(page.url or "")

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

    # ── Проверяем, не ушли ли мы на другой сайт или другую страницу ──
    # Если URL изменился — это был навигационный CTA, а не gate. Откатываемся.
    after_url = str(page.url or "")
    if after_url != before_url:
        navigated_offsite = not is_same_site_url(after_url, before_url)
        navigated_to_other_page = is_navigation_like_href(after_url, before_url)
        if navigated_offsite or navigated_to_other_page:
            logger.warning(
                f"⛔ Smart cursor: entry-клик вызвал навигацию ({before_url} → {after_url}), откатываемся"
            )
            try:
                await page.go_back(timeout=8000, wait_until="domcontentloaded")
                await page.wait_for_timeout(600)
            except Exception:
                try:
                    await page.goto(before_url, wait_until="domcontentloaded", timeout=15000)
                    await page.wait_for_timeout(600)
                except Exception:
                    pass
            return cursor_pos, False, None

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
    _default = {
        "scrollY": 0,
        "maxScroll": 0,
        "atBottom": False,
        "documentHeight": 0,
        "viewportHeight": 0,
        "bottomGap": 0,
    }
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
                const rootHeights = [
                    document.documentElement?.scrollHeight || 0,
                    document.documentElement?.offsetHeight || 0,
                    document.documentElement?.clientHeight || 0,
                    document.body?.scrollHeight || 0,
                    document.body?.offsetHeight || 0,
                    document.body?.clientHeight || 0,
                    scrollingEl?.scrollHeight || 0,
                ];

                let scrollY = Math.max(
                    toInt(window.scrollY || window.pageYOffset || 0, 0),
                    toInt(scrollingEl?.scrollTop || 0, 0),
                );
                let documentHeight = Math.max(
                    viewportHeight,
                    ...rootHeights.map((value) => toInt(value, 0)),
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

                    documentHeight = Math.max(
                        documentHeight,
                        toInt(rect.bottom + scrollY, 0),
                        toInt((el.offsetTop || 0) + elementHeight, 0),
                    );

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

                maxScroll = Math.max(maxScroll, Math.max(0, documentHeight - viewportHeight));
                const bottomGap = Math.max(0, documentHeight - (scrollY + viewportHeight));
                const atBottom = (
                    documentHeight <= viewportHeight + 4
                    || bottomGap <= 8
                    || (maxScroll > 4 && scrollY >= (maxScroll - 6))
                );
                return {
                    scrollY,
                    maxScroll,
                    atBottom,
                    documentHeight,
                    viewportHeight,
                    bottomGap,
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


def metrics_viewport_height(metrics: Dict[str, Any], fallback_viewport_height: int) -> int:
    metric_viewport_height = _to_float(metrics.get("viewportHeight"), fallback_viewport_height)
    if metric_viewport_height <= 0:
        metric_viewport_height = max(1.0, float(fallback_viewport_height or 1))
    return max(1, int(metric_viewport_height))


def metrics_document_height(metrics: Dict[str, Any], fallback_viewport_height: int) -> int:
    metric_viewport_height = metrics_viewport_height(metrics, fallback_viewport_height)
    document_height = max(
        metric_viewport_height,
        int(_to_float(metrics.get("documentHeight"), 0.0)),
        int(_to_float(metrics.get("maxScroll"), 0.0)) + metric_viewport_height,
    )
    return max(metric_viewport_height, document_height)


def confirm_bottom_state_from_metrics(
    metrics: Dict[str, Any],
    viewport_height: int,
    analysis_max_abs_y: int = 0,
    round_index: int = 0,
    bottom_stable_rounds_required: int = 4,
    stagnant_rounds: int = 0,
) -> Tuple[bool, str]:
    current_scroll_y = max(0, int(_to_float(metrics.get("scrollY"), 0.0)))
    max_scroll_y = max(0, int(_to_float(metrics.get("maxScroll"), 0.0)))
    metric_viewport_height = metrics_viewport_height(metrics, viewport_height)
    document_height = metrics_document_height(metrics, metric_viewport_height)
    bottom_gap = max(
        0,
        int(_to_float(metrics.get("bottomGap"), document_height - (current_scroll_y + metric_viewport_height))),
    )
    at_bottom = bool(metrics.get("atBottom", False))

    analysis_gap: Optional[int] = None
    if analysis_max_abs_y > 0:
        analysis_gap = int(analysis_max_abs_y - (current_scroll_y + metric_viewport_height * 0.94))

    doc_gap_threshold = max(20, int(metric_viewport_height * 0.12))
    analysis_gap_threshold = max(24, int(metric_viewport_height * 0.10))

    if not at_bottom:
        if max_scroll_y > 12:
            return False, f"window distance={max_scroll_y - current_scroll_y}"
        if document_height > metric_viewport_height + 40:
            return False, f"document gap={bottom_gap}"
        if analysis_gap is not None:
            return False, f"analysis tail={analysis_gap}"
        return False, "atBottom=false"

    if max_scroll_y > 12 and current_scroll_y < (max_scroll_y - 8):
        return False, f"window distance={max_scroll_y - current_scroll_y}"

    if document_height > metric_viewport_height + 40 and bottom_gap > doc_gap_threshold:
        return False, f"document gap={bottom_gap}"

    if analysis_gap is not None and analysis_gap > analysis_gap_threshold:
        return False, f"analysis tail={analysis_gap}"

    if max_scroll_y > 12:
        return True, f"window distance={max_scroll_y - current_scroll_y}"
    if document_height > metric_viewport_height + 40:
        return True, f"document gap={bottom_gap}"
    if analysis_gap is not None:
        return True, f"analysis tail={analysis_gap}"

    fallback_confirmed = round_index >= max(8, bottom_stable_rounds_required * 2) and stagnant_rounds >= 1
    return (
        fallback_confirmed,
        "fallback "
        f"guard_round={round_index >= max(8, bottom_stable_rounds_required * 2)} "
        f"guard_stagnant={stagnant_rounds >= 1}",
    )


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


async def wait_for_deep_page_ready(
    page: Any,
    load_timeout_ms: int,
    post_networkidle_pause_ms: int = 10000,
) -> bool:
    """Ждёт глубокую загрузку страницы: networkidle и дополнительную паузу для JS-анимаций."""
    networkidle_timeout = max(1000, int(load_timeout_ms))
    post_pause = max(0, int(post_networkidle_pause_ms))
    networkidle_reached = False

    logger.info("⏳ Deep load: ждём networkidle перед стартом записи")
    try:
        await page.wait_for_load_state("networkidle", timeout=networkidle_timeout)
        networkidle_reached = True
        logger.info("✅ Deep load: networkidle достигнут")
    except Exception:
        logger.warning(
            f"⚠️ Deep load: networkidle не достигнут за {networkidle_timeout}ms, продолжаем с grace-паузой"
        )

    if post_pause > 0:
        logger.info(
            f"⏳ Deep load: дополнительная пауза {post_pause // 1000}s для инициализации JS-анимаций"
        )
        try:
            await page.wait_for_timeout(post_pause)
        except Exception:
            pass

    return networkidle_reached


async def perform_prerender_scroll(page: Any) -> None:
    """Мягкий толчок для инициализации нативных scroll-скриптов без прыжка страницы."""
    logger.info("🔄 Pre-render soft init: wheel(150) → sleep 1.5s")
    try:
        await page.mouse.wheel(0, 150)
        await asyncio.sleep(1.5)
    except Exception as exc:
        logger.warning(f"⚠️ Pre-render scroll: {exc}")


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


def nav_blacklist_key(target: Dict[str, Any]) -> str:
    """Ключ для чёрного списка навигации — объединяет похожие элементы независимо от позиции.

    Если кнопка с текстом 'Read More' вызвала навигацию, ВСЕ кнопки 'Read More'
    на странице будут пропущены. Работает по tag+text+href паттерну.
    """
    tag = str(target.get("tag", "")).strip().lower()
    text = interaction_text(target)[:24]
    href = str(target.get("href", "")).strip().lower().split("?")[0][:36]
    return f"NAV|{tag}|{text}|{href}"


def is_nav_blacklisted(target: Dict[str, Any], blacklist: Optional[Set[str]]) -> bool:
    """Проверяет, находится ли элемент в чёрном списке навигации."""
    if not blacklist:
        return False
    return nav_blacklist_key(target) in blacklist


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


async def try_close_cookie_banner(
    page: Any,
    cursor_pos: Tuple[float, float],
    viewport_width: int,
    viewport_height: int,
) -> Tuple[Tuple[float, float], bool]:
    """Ищет и закрывает cookie-баннеры по универсальным эвристикам.

    Правило: ищем элементы (button, a, div) с z-index > 100,
    позицией fixed/absolute, текст содержит cookie-related ключевые слова
    на разных языках.
    """
    try:
        cookie_target = await page.evaluate(
            """
            ({ viewportWidth, viewportHeight }) => {
                const cookieAcceptWords = [
                    'accept', 'accept all', 'allow', 'allow all', 'got it', 'agree',
                    'ok', 'okay', 'i understand', 'understood', 'dismiss', 'close',
                    'принять', 'згоден', 'согласен', 'понятно', 'хорошо',
                    'accepter', 'tout accepter', 'akzeptieren', 'alle akzeptieren',
                    'aceptar', 'aceptar todo', 'aceitar', 'accetta', 'accetta tutto',
                    'aanvaarden', 'acceptera', 'godkänn',
                ];

                const bannerId = /cookie|consent|gdpr|privacy|cc-|cmp-|onetrust|cookiebot|klaro|tarteaucitron|osano|axeptio/i;

                const candidates = [];
                const interactives = document.querySelectorAll(
                    'button, a, div[role="button"], span[role="button"], '
                    + '[class*="cookie"] button, [class*="consent"] button, '
                    + '[class*="cookie"] a, [class*="consent"] a, '
                    + '[id*="cookie"] button, [id*="consent"] button, '
                    + '[class*="accept"], [class*="agree"], [class*="dismiss"]'
                );

                for (const el of interactives) {
                    if (!(el instanceof HTMLElement)) continue;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden'
                        || Number(style.opacity || '1') < 0.05) continue;

                    const rect = el.getBoundingClientRect();
                    if (rect.width < 30 || rect.height < 16) continue;
                    if (rect.bottom < 0 || rect.top > viewportHeight) continue;

                    const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                    if (!text || text.length > 80) continue;

                    const matchesKeyword = cookieAcceptWords.some(kw => text.includes(kw));
                    if (!matchesKeyword) continue;

                    // Проверяем, что элемент или его предок — баннероподобный контейнер
                    let bannerLike = false;
                    let current = el;
                    let depth = 0;
                    while (current && current instanceof HTMLElement && depth < 10) {
                        const cs = window.getComputedStyle(current);
                        const pos = cs.position;
                        const z = parseInt(cs.zIndex) || 0;
                        const isFixed = (pos === 'fixed' || pos === 'sticky');
                        const isAbsolute = pos === 'absolute';
                        const isHighZ = z > 100;

                        // Баннер обычно внизу или вверху экрана
                        const cRect = current.getBoundingClientRect();
                        const atBottom = cRect.bottom > viewportHeight * 0.55;
                        const atTop = cRect.top < viewportHeight * 0.35;

                        if ((isFixed || isAbsolute || isHighZ) && (atBottom || atTop)) {
                            bannerLike = true;
                            break;
                        }

                        // Распознаём по class/id паттернам
                        const hint = ((current.className || '').toString()
                            + ' ' + (current.id || '')).toLowerCase();
                        if (bannerId.test(hint)) {
                            bannerLike = true;
                            break;
                        }
                        current = current.parentElement;
                        depth++;
                    }

                    if (!bannerLike) continue;

                    const cx = rect.left + rect.width / 2;
                    const cy = rect.top + rect.height / 2;

                    candidates.push({
                        x: Math.max(2, Math.min(viewportWidth - 2, cx)),
                        y: Math.max(2, Math.min(viewportHeight - 2, cy)),
                        text: text.slice(0, 40),
                        score: (style.cursor === 'pointer' ? 60 : 0)
                            + (rect.width * rect.height > 2000 ? 40 : 0)
                            + (/accept|принять|agree|zgod/i.test(text) ? 80 : 0)
                    });
                }

                if (candidates.length === 0) return null;
                candidates.sort((a, b) => b.score - a.score);
                return candidates[0];
            }
            """,
            {"viewportWidth": viewport_width, "viewportHeight": viewport_height},
        )
    except Exception as exc:
        if _is_nav_error(exc):
            await _recover_after_nav(page)
        return cursor_pos, False

    if not cookie_target:
        return cursor_pos, False

    tx = float(cookie_target["x"])
    ty = float(cookie_target["y"])
    text_hint = str(cookie_target.get("text", "")).strip()

    logger.info(f"🍪 Обнаружен cookie-баннер: '{text_hint[:30]}', закрываем...")

    cursor_pos = await move_mouse_human_like(
        page, cursor_pos, (tx, ty),
        viewport_width, viewport_height,
        random.randint(300, 700),
    )
    await page.wait_for_timeout(random.randint(100, 300))
    await page.mouse.click(tx, ty, delay=random.randint(40, 100))
    await page.wait_for_timeout(random.randint(500, 1200))

    logger.info("✅ Cookie-баннер закрыт")
    return cursor_pos, True


async def find_viewport_cta_elements(
    page: Any,
    viewport_width: int,
    viewport_height: int,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """Находит CTA-кнопки в текущем viewport с учётом focus-zone и контрастности."""
    try:
        return await page.evaluate(
            """
            ({ viewportWidth, viewportHeight, limit }) => {
                const ctaKeywords = [
                    'get started', 'start', 'download', 'contact', 'buy', 'sign up',
                    'join', 'try', 'learn more', 'discover', 'explore', 'book',
                    'schedule', 'demo', 'pricing', 'quote', 'request', 'subscribe',
                    'apply', 'shop', 'free trial', "let's talk", 'lets talk',
                    'начать', 'попробовать', 'купить', 'скачать', 'подписаться',
                    'записаться', 'связаться', 'заказать',
                ];

                function getLuminance(rgb) {
                    const match = rgb.match(/\\d+/g);
                    if (!match || match.length < 3) return 0.5;
                    const [r, g, b] = match.map(Number);
                    const sR = r / 255, sG = g / 255, sB = b / 255;
                    const R = sR <= 0.03928 ? sR / 12.92 : Math.pow((sR + 0.055) / 1.055, 2.4);
                    const G = sG <= 0.03928 ? sG / 12.92 : Math.pow((sG + 0.055) / 1.055, 2.4);
                    const B = sB <= 0.03928 ? sB / 12.92 : Math.pow((sB + 0.055) / 1.055, 2.4);
                    return 0.2126 * R + 0.7152 * G + 0.0722 * B;
                }

                function hasContrastWithParent(el) {
                    const style = window.getComputedStyle(el);
                    const bgColor = style.backgroundColor;
                    if (!bgColor || bgColor === 'transparent'
                        || bgColor === 'rgba(0, 0, 0, 0)') return false;
                    let parent = el.parentElement;
                    let parentBg = 'rgba(0, 0, 0, 0)';
                    let depth = 0;
                    while (parent && depth < 6) {
                        const pStyle = window.getComputedStyle(parent);
                        const pBg = pStyle.backgroundColor;
                        if (pBg && pBg !== 'transparent' && pBg !== 'rgba(0, 0, 0, 0)') {
                            parentBg = pBg;
                            break;
                        }
                        parent = parent.parentElement;
                        depth++;
                    }
                    if (parentBg === 'rgba(0, 0, 0, 0)') parentBg = 'rgb(255, 255, 255)';
                    const elLum = getLuminance(bgColor);
                    const parentLum = getLuminance(parentBg);
                    const ratio = (Math.max(elLum, parentLum) + 0.05)
                        / (Math.min(elLum, parentLum) + 0.05);
                    return ratio >= 2.0;
                }

                function hasFixedOrStickyAncestor(node) {
                    let depth = 0;
                    while (node && depth < 10) {
                        if (node.nodeType !== 1) break;
                        const pos = window.getComputedStyle(node).position;
                        if (pos === 'fixed' || pos === 'sticky') return true;
                        node = node.parentElement;
                        depth++;
                    }
                    return false;
                }

                const focusTop = viewportHeight * 0.25;
                const focusBottom = viewportHeight * 0.75;
                const centerX = viewportWidth / 2;
                const centerY = viewportHeight / 2;
                const diag = Math.sqrt(centerX * centerX + centerY * centerY);

                const candidates = [];
                const elements = document.querySelectorAll(
                    'button, a, [role="button"], [class*="btn"], [class*="cta"], [class*="action"]'
                );

                for (const el of elements) {
                    if (!(el instanceof HTMLElement)) continue;

                    const style = window.getComputedStyle(el);
                    if (style.position === 'fixed' || style.position === 'sticky') continue;
                    if (hasFixedOrStickyAncestor(el.parentElement)) continue;
                    if (style.display === 'none' || style.visibility === 'hidden'
                        || Number(style.opacity || '1') < 0.05) continue;
                    if (style.pointerEvents === 'none') continue;

                    const rect = el.getBoundingClientRect();
                    if (rect.width < 50 || rect.height < 18) continue;
                    if (rect.right < 0 || rect.left > viewportWidth) continue;
                    if (rect.bottom < 0 || rect.top > viewportHeight) continue;

                    const cx = rect.left + rect.width / 2;
                    const cy = rect.top + rect.height / 2;
                    if (!Number.isFinite(cx) || !Number.isFinite(cy)) continue;
                    if (cy < focusTop || cy > focusBottom) continue;

                    const text = (el.innerText || '').trim().toLowerCase();
                    if (text.length < 2 || text.length > 50) continue;

                    const hasKeyword = ctaKeywords.some(kw => text.includes(kw));
                    const hasContrast = hasContrastWithParent(el);

                    if (!hasKeyword && !hasContrast) continue;

                    const area = rect.width * rect.height;
                    const distFromCenter = Math.sqrt(
                        (cx - centerX) * (cx - centerX) + (cy - centerY) * (cy - centerY)
                    );
                    const centerScore = Math.max(0, 1 - distFromCenter / (diag * 0.5)) * 120;
                    const areaScore = Math.min(area / 500, 60);

                    const href = (el.tagName.toLowerCase() === 'a'
                        ? (el.getAttribute('href') || '') : '').trim();

                    candidates.push({
                        x: Math.max(2, Math.min(viewportWidth - 2, cx)),
                        y: Math.max(2, Math.min(viewportHeight - 2, cy)),
                        text: text.slice(0, 40),
                        dedupKey: text.slice(0, 40) + '|' + href.slice(0, 80),
                        hasKeyword: hasKeyword,
                        hasContrast: hasContrast,
                        score: (hasKeyword ? 100 : 0)
                            + (hasContrast ? 80 : 0)
                            + (style.cursor === 'pointer' ? 30 : 0)
                            + centerScore
                            + areaScore
                    });
                }

                candidates.sort((a, b) => b.score - a.score);
                return candidates.slice(0, limit);
            }
            """,
            {"viewportWidth": viewport_width, "viewportHeight": viewport_height, "limit": limit},
        )
    except Exception as exc:
        if _is_nav_error(exc):
            await _recover_after_nav(page)
        return []


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
    nav_blacklist: Optional[Set[str]] = None,
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
            if is_nav_blacklisted(item, nav_blacklist):
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
            # Добавляем в чёрный список навигации
            if nav_blacklist is not None:
                _bl_key = nav_blacklist_key(follow_target)
                nav_blacklist.add(_bl_key)
                logger.info(f"🚫 Nav blacklist: добавлен '{_bl_key}' (внешний переход из followup)")
            await ensure_page_within_allowed_site(
                page,
                allowed_url,
                fallback_url=before_url,
                fallback_scroll_y=scroll_y,
                timeout=12000,
            )
            break

        if navigated_internally and not allow_internal_nav_click:
            if nav_blacklist is not None:
                _bl_key = nav_blacklist_key(follow_target)
                nav_blacklist.add(_bl_key)
                logger.info(f"🚫 Nav blacklist: добавлен '{_bl_key}' (внутренний переход из followup)")
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
    total_delta = int(viewport_height * random.uniform(0.30, 0.50) * scroll_speed_factor)
    steps = random.randint(5, 9)
    base_step = max(30, int(total_delta / max(steps, 1)))

    for _ in range(steps):
        step_delta = max(24, int(base_step + random.uniform(-8, 12)))
        await page.mouse.wheel(0, step_delta)
        await page.wait_for_timeout(random.randint(max(35, scroll_pause_min_ms), max(55, scroll_pause_max_ms)))


async def perform_gsap_micro_scroll(
    page: Any,
    viewport_height: int,
    scroll_speed_factor: float,
    scroll_pause_min_ms: int,
    scroll_pause_max_ms: int,
) -> None:
    """Микро-скролл для GSAP/ScrollTrigger сайтов: много мелких wheel-событий
    с короткими паузами вместо редких крупных прыжков.  Это заставляет GSAP
    плавно прокручивать анимации в pinned-секциях, как тачпад реального юзера."""
    # 12-20 тиков по 30px — плавный тачпад-скролл, GSAP обрабатывает каждое событие
    n_ticks = random.randint(12, 20)
    tick_size_base = 30  # px за один wheel-event — имитация тачпада

    # При замедлении (slowdown на секциях) уменьшаем тик до ~20px, GSAP успевает сыграть сцену
    tick_size = max(18, int(tick_size_base * clamp(scroll_speed_factor, 0.40, 1.20)))

    for i in range(n_ticks):
        step = max(16, int(tick_size + random.uniform(-6, 8)))
        try:
            await page.mouse.wheel(0, step)
        except Exception:
            pass
        # 40-55мс — 1-2 кадра видео между событиями, GSAP успевает отрисовать
        await page.wait_for_timeout(random.randint(40, 55))


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

            function isContentAnchorCandidate(el, tag, textNoWs, rect) {
                const cls = (el.className || '').toString().toLowerCase();
                const idPart = (el.id || '').toString().toLowerCase();
                const hint = `${cls} ${idPart}`;

                const semanticBlock = ['main', 'section', 'article', 'footer'].includes(tag);
                if (semanticBlock && rect.height >= Math.max(40, viewportHeight * 0.08)) {
                    return true;
                }

                if (['h1', 'h2', 'h3', 'h4'].includes(tag)) {
                    return textNoWs.length >= 10 && rect.width >= viewportWidth * 0.22;
                }

                const explicitSectionHint = /hero|feature|pricing|faq|footer|testimonial|review|integrat|cta|benefit|advantage|section/.test(hint);
                if (explicitSectionHint && rect.width >= viewportWidth * 0.28 && rect.height >= Math.max(24, viewportHeight * 0.05)) {
                    return true;
                }

                const textBlock = ['p', 'blockquote', 'li'].includes(tag);
                if (textBlock && textNoWs.length >= 20 && rect.width >= viewportWidth * 0.22 && rect.height >= 18) {
                    return true;
                }

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
                const contentAnchor = (!interactive && !hoverText && !surfaceHover)
                    ? isContentAnchorCandidate(el, tag, textNoWs, rect)
                    : false;
                if (!interactive && !hoverText && !surfaceHover && !contentAnchor) continue;

                const key = elementKey(el, absX, absY, text);

                const pointerBoost = style.cursor === 'pointer' ? 90 : 0;
                const tagBoost = ({ button: 100, a: 90, input: 70, select: 70, textarea: 65, video: 55, canvas: 55 })[tag] || 45;
                const textBoost = text.length > 0 ? 24 : 0;
                const areaBoost = Math.min(rect.width * rect.height, 12000) * 0.01;
                const hoverBoost = hoverText ? 82 : 0;
                const surfaceBoost = surfaceHover ? 106 : 0;
                const anchorBoost = contentAnchor ? 8 : 0;

                out.push({
                    key,
                    x: clampValue(absX, 2, viewportWidth - 2),
                    absY: Math.max(1, absY),
                    width: rect.width,
                    height: rect.height,
                    score: pointerBoost + tagBoost + textBoost + areaBoost + hoverBoost + surfaceBoost + anchorBoost,
                    text,
                    href,
                    tag,
                    isHoverText: hoverText,
                    isSurfaceHover: surfaceHover,
                    isContentAnchor: contentAnchor,
                    visibleRatio,
                    visibilityClarity,
                });
            }

            out.sort((a, b) => (a.absY - b.absY) || (b.score - a.score));
            const maxItems = Math.max(limit, 1);
            if (out.length <= maxItems) return out;

            const bandHeight = Math.max(220, Math.floor(viewportHeight * 0.85));
            const bandCount = Math.max(1, Math.ceil(docHeight / bandHeight));
            const perBand = Math.max(4, Math.ceil(maxItems / bandCount));
            const bands = Array.from({ length: bandCount }, () => []);

            for (const item of out) {
                const bandIndex = Math.max(0, Math.min(bandCount - 1, Math.floor(item.absY / bandHeight)));
                bands[bandIndex].push(item);
            }

            const sampled = [];
            const seenKeys = new Set();
            for (const bandItems of bands) {
                if (!bandItems.length) continue;
                bandItems.sort((a, b) => {
                    const anchorOrder = (a.isContentAnchor ? 1 : 0) - (b.isContentAnchor ? 1 : 0);
                    return anchorOrder || (b.score - a.score) || (a.absY - b.absY);
                });
                for (const item of bandItems.slice(0, perBand)) {
                    if (seenKeys.has(item.key)) continue;
                    seenKeys.add(item.key);
                    sampled.push(item);
                }
            }

            if (sampled.length < maxItems) {
                const remainder = [...out].sort((a, b) => {
                    const anchorOrder = (a.isContentAnchor ? 1 : 0) - (b.isContentAnchor ? 1 : 0);
                    return anchorOrder || (b.score - a.score) || (a.absY - b.absY);
                });
                for (const item of remainder) {
                    if (sampled.length >= maxItems) break;
                    if (seenKeys.has(item.key)) continue;
                    seenKeys.add(item.key);
                    sampled.push(item);
                }
            }

            sampled.sort((a, b) => (a.absY - b.absY) || (b.score - a.score));
            return sampled.slice(0, maxItems);
        }
        """,
        {
            "viewportWidth": viewport_width,
            "viewportHeight": viewport_height,
            "limit": limit,
        },
    )


async def collect_css_hover_map(
    page: Any,
    viewport_width: int,
    viewport_height: int,
) -> List[Dict[str, Any]]:
    """Сканирует CSS-правила всех таблиц стилей и возвращает элементы с hover/transition/pointer.

    Вызывается один раз после загрузки (до начала основного цикла) чтобы построить
    полную карту взаимодействий ещё до старта движения курсора.
    """
    try:
        return await page.evaluate(
            """
            ({ viewportWidth, viewportHeight }) => {
                // ── 1. Читаем CSS-правила из всех доступных таблиц стилей ──
                const hoverSelectors = new Set();
                const sheets = Array.from(document.styleSheets || []);
                for (const sheet of sheets) {
                    let rules;
                    try { rules = Array.from(sheet.cssRules || []); } catch { continue; }
                    for (const rule of rules) {
                        // @media, @supports и т.п. — рекурсивно
                        const innerRules = rule.cssRules ? Array.from(rule.cssRules) : [rule];
                        for (const r of innerRules) {
                            if (!r.selectorText) continue;
                            const sel = r.selectorText;
                            const style = r.style || {};
                            const hasCursorPointer = (style.cursor || '').toLowerCase() === 'pointer';
                            const hasTransition = (style.transitionDuration || '') !== ''
                                && (style.transitionDuration || '') !== '0s'
                                && (style.transitionDuration || '') !== '0ms';
                            const hasHoverPseudo = /:hover|:focus|:active|:focus-visible/.test(sel);
                            if (hasCursorPointer || hasTransition || hasHoverPseudo) {
                                // Убираем псевдоэлементы — нам нужны базовые CSS-селекторы
                                const baseSelector = sel
                                    .split(',')
                                    .map(s => s.replace(/::?[a-z\\-]+(\\([^)]*\\))?/gi, '').trim())
                                    .filter(s => s.length > 0 && s !== '*')
                                    .join(',');
                                if (baseSelector) {
                                    try {
                                        // Проверяем, что селектор валиден
                                        document.querySelector(baseSelector);
                                        hoverSelectors.add(baseSelector);
                                    } catch {}
                                }
                            }
                        }
                    }
                }

                // ── 2. Фиксированный набор семантически-интерактивных тегов ──
                const semanticSelectors = [
                    'a[href]', 'button', 'input:not([type="hidden"])', 'select', 'textarea',
                    'details', 'summary', 'label[for]',
                    '[onclick]', '[role="button"]', '[role="link"]', '[role="menuitem"]',
                    '[tabindex]:not([tabindex="-1"])', '[contenteditable="true"]',
                    '[draggable="true"]', '[data-action]', '[data-hover]',
                    '[class*="btn"]', '[class*="cta"]',
                ];
                for (const s of semanticSelectors) hoverSelectors.add(s);

                // ── 3. Собираем уникальные элементы ──
                const pool = new Set();
                for (const sel of hoverSelectors) {
                    try {
                        for (const el of document.querySelectorAll(sel)) {
                            if (el instanceof HTMLElement) pool.add(el);
                        }
                    } catch {}
                }

                function hasTransitionStyle(style) {
                    const raw = (style.transitionDuration || '').toString();
                    if (!raw) return false;
                    for (const part of raw.split(',')) {
                        const token = part.trim().toLowerCase();
                        if (!token) continue;
                        if (token.endsWith('ms') && Number.parseFloat(token) > 0.1) return true;
                        if (token.endsWith('s') && Number.parseFloat(token) > 0.001) return true;
                    }
                    return false;
                }

                const scrollY = window.scrollY || 0;
                const doc = document.documentElement;
                const docHeight = Math.max(doc.scrollHeight || 0, document.body.scrollHeight || 0, viewportHeight);
                const out = [];

                for (const el of pool) {
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden'
                        || Number(style.opacity || '1') < 0.05) continue;
                    if (style.pointerEvents === 'none') continue;

                    const rect = el.getBoundingClientRect();
                    if (rect.width < 8 || rect.height < 8) continue;

                    const absY = scrollY + rect.top + rect.height * 0.5;
                    const absX = rect.left + rect.width * 0.5;

                    const hasPointer = (style.cursor || '').toLowerCase() === 'pointer';
                    const hasTrans = hasTransitionStyle(style);
                    const tag = el.tagName.toLowerCase();
                    const isInteractive = ['a', 'button', 'input', 'select', 'textarea', 'summary'].includes(tag)
                        || el.hasAttribute('onclick') || el.getAttribute('role') === 'button';

                    const score = (hasPointer ? 80 : 0) + (hasTrans ? 60 : 0) + (isInteractive ? 40 : 0)
                        + Math.min(rect.width * rect.height, 60000) * 0.001;

                    const text = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim().slice(0, 60);
                    const href = el instanceof HTMLAnchorElement ? (el.getAttribute('href') || '') : '';

                    // Собираем ключ элемента
                    let keyParts = [];
                    let cur = el;
                    let depth = 0;
                    while (cur && cur instanceof HTMLElement && depth < 4) {
                        const cls = (cur.className || '').toString().trim().split(/\\s+/).slice(0, 2).join('.');
                        keyParts.push(`${cur.tagName.toLowerCase()}${cur.id ? '#' + cur.id : ''}${cls ? '.' + cls : ''}`);
                        cur = cur.parentElement;
                        depth++;
                    }
                    const key = `${keyParts.join('>')}|${Math.round(absX)}:${Math.round(absY)}|${text.slice(0, 30)}`;

                    out.push({
                        key,
                        tag,
                        x: absX,
                        absY,
                        y: rect.top + rect.height * 0.5,
                        width: rect.width,
                        height: rect.height,
                        text,
                        href,
                        score,
                        isHoverText: hasTrans && !isInteractive && rect.width < viewportWidth * 0.5,
                        isSurfaceHover: hasTrans && rect.width >= viewportWidth * 0.16,
                        hasPointerCursor: hasPointer,
                        hasTransition: hasTrans,
                        isInteractive,
                        isContentAnchor: false,
                        visibleRatio: 1.0,
                        clarityScore: 1.0,
                    });
                }

                // Сортируем: сначала верхние (по absY), потом левые (по x)
                out.sort((a, b) => (a.absY - b.absY) || (a.x - b.x));
                return out.slice(0, 1500);
            }
            """,
            {"viewportWidth": viewport_width, "viewportHeight": viewport_height},
        )
    except Exception:
        return []


async def run_header_hover_pass(
    page: Any,
    cursor_pos: Tuple[float, float],
    viewport_width: int,
    viewport_height: int,
    hover_min_ms: int,
    hover_max_ms: int,
) -> Tuple[Tuple[float, float], int]:
    """Быстрый проход по hover-элементам в зоне хедера (верхние 12% экрана).

    Вызывается в самом начале записи чтобы показать эффекты хедера и навигации.
    Только hover — без кликов.
    """
    hovered = 0
    try:
        header_targets = await page.evaluate(
            """
            ({ viewportWidth, viewportHeight }) => {
                const headerZoneBottom = viewportHeight * 0.14;
                const pool = new Set();
                // Верхушка: header, nav, fixed/sticky элементы
                for (const sel of [
                    'header *', 'nav *', '[role="navigation"] *',
                    '[class*="nav"] *', '[class*="header"] *', '[class*="menu"] *',
                ]) {
                    try {
                        for (const el of document.querySelectorAll(sel)) {
                            if (el instanceof HTMLElement) pool.add(el);
                        }
                    } catch {}
                }
                // Любые видимые интерактивные элементы в верхней зоне
                for (const el of document.querySelectorAll('a[href],button,[role="button"],[onclick]')) {
                    if (!(el instanceof HTMLElement)) continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.top >= 0 && rect.bottom <= headerZoneBottom * 1.5) pool.add(el);
                }

                const out = [];
                for (const el of pool) {
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden'
                        || Number(style.opacity || '1') < 0.05) continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 8 || rect.height < 8) continue;
                    if (rect.bottom > headerZoneBottom * 1.5) continue;
                    if (rect.top < 0) continue;

                    const hasHover = (style.cursor || '').toLowerCase() === 'pointer'
                        || (style.transitionDuration || '') !== ''
                        || el.tagName.toLowerCase() === 'a'
                        || el.tagName.toLowerCase() === 'button';
                    if (!hasHover) continue;

                    const cx = rect.left + rect.width * 0.5;
                    const cy = rect.top + rect.height * 0.5;
                    out.push({ x: cx, y: cy, width: rect.width, height: rect.height,
                               text: (el.innerText || '').trim().slice(0, 40) });
                }

                // Сортируем слева направо
                out.sort((a, b) => a.x - b.x);
                // Убираем близкие дубли (< 40px)
                const deduped = [];
                for (const item of out) {
                    if (!deduped.length || Math.abs(item.x - deduped[deduped.length - 1].x) > 40) {
                        deduped.push(item);
                    }
                }
                return deduped.slice(0, 10);
            }
            """,
            {"viewportWidth": viewport_width, "viewportHeight": viewport_height},
        )
    except Exception:
        return cursor_pos, 0

    if not header_targets:
        return cursor_pos, 0

    # Показываем до 8 элементов хедера: ховеры навигации важны для записи
    header_targets = header_targets[:8]
    logger.info(f"🧭 Header hover pass: {len(header_targets)} элементов в зоне хедера")

    for item in header_targets:
        tx = clamp(float(item.get("x", viewport_width * 0.5)), 2, viewport_width - 2)
        ty = clamp(float(item.get("y", viewport_height * 0.06)), 2, viewport_height - 2)
        # Быстрое движение + достаточный dwell чтобы CSS :hover триггернулся на видео
        transit_ms = random.randint(18, 40)
        dwell_ms = random.randint(80, 180)
        try:
            cursor_pos = await move_mouse_human_like(
                page, cursor_pos, (tx, ty),
                viewport_width, viewport_height, transit_ms,
            )
            await page.wait_for_timeout(dwell_ms)
            hovered += 1
        except Exception:
            pass

    return cursor_pos, hovered


async def detect_preloader(page: Any, viewport_width: int, viewport_height: int) -> bool:
    """Возвращает True если страница сейчас показывает прелоадер или сплэш-экран."""
    try:
        result = await page.evaluate(
            """
            ({ vw, vh }) => {
                // Смотрим — есть ли элемент, покрывающий > 70% экрана
                function covers(el) {
                    const rect = el.getBoundingClientRect();
                    const overlapW = Math.max(0, Math.min(rect.right, vw) - Math.max(rect.left, 0));
                    const overlapH = Math.max(0, Math.min(rect.bottom, vh) - Math.max(rect.top, 0));
                    return (overlapW * overlapH) >= vw * vh * 0.70;
                }

                // Проверяем children body
                let hasCovering = false;
                for (const el of document.body.children) {
                    if (!(el instanceof HTMLElement)) continue;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none') continue;
                    if (covers(el)) {
                        const zIndex = Number(style.zIndex) || 0;
                        const hasAnim = (style.animationName || '') !== 'none' || (style.transitionDuration || '') !== '0s';
                        // Прелоадер-признаки: перекрывает экран + z-index высокий или позиция fixed
                        if (style.position === 'fixed' || style.position === 'absolute' || zIndex >= 100 || hasAnim) {
                            hasCovering = true;
                        }
                    }
                }

                // Кол-во значимых интерактивных элементов
                const interactiveCount = document.querySelectorAll(
                    'a[href], button, nav, header, main, [role="navigation"]'
                ).length;

                // Если страница не скроллируется — тоже плохой знак
                const scrollable = (document.documentElement.scrollHeight || 0) > vh * 1.4;

                return { hasCovering, interactiveCount, scrollable };
            }
            """,
            {"vw": viewport_width, "vh": viewport_height},
        )
        has_covering = bool(result.get("hasCovering", False))
        interactive = int(result.get("interactiveCount", 99))
        scrollable = bool(result.get("scrollable", True))
        # Прелоадер: покрывает экран И мало интерактивных элементов ИЛИ нет прокрутки
        return has_covering and (interactive < 8 or not scrollable)
    except Exception:
        return False


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
    nav_blacklist: Optional[Set[str]] = None,
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
        # "try" убрано — слишком часто триггерит CTA «TRY FOR FREE»
        "view", "explore", "activate", "continue", "enter",
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

    # ── Universal origin guard: запоминаем URL с которого начали pass ──
    # После ЛЮБОГО клика, если path изменился — немедленно возвращаемся на origin.
    # Это защищает от случайных переходов по CTA независимо от allow_internal_nav_click.
    pass_origin_url = str(page.url or site_url)
    pass_origin_path = _url_page_path(pass_origin_url)

    last_scroll_y = -1
    stagnant_rounds = 0
    bottom_stable_rounds = 0
    round_index = 0
    # Скрываем чат-виджеты, которые могли открыться при загрузке страницы
    try:
        await try_close_chat_widgets(page)
    except Exception:
        pass

    # ── Pre-scroll для прогрева lazy-load контента ──
    # Быстро прокручиваем страницу вниз/вверх программно (без курсора),
    # чтобы все ленивые изображения, видео и анимации загрузились до начала записи.
    logger.info("🔄 Pre-scroll: прогреваем lazy-load контент...")
    try:
        _prescroll_steps = 8
        for _ps_i in range(_prescroll_steps):
            await page.evaluate(
                """(step) => {
                    const vh = window.innerHeight || 800;
                    window.scrollBy({ top: vh * 0.85, left: 0, behavior: 'auto' });
                }""",
                _ps_i,
            )
            await page.wait_for_timeout(random.randint(75, 150))
        # Возвращаемся наверх
        await page.evaluate("""() => window.scrollTo({ top: 0, left: 0, behavior: 'auto' })""")
        await page.wait_for_timeout(random.randint(100, 200))
        logger.info("✅ Pre-scroll завершён")
    except Exception as _prescroll_exc:
        logger.warning(f"⚠️ Pre-scroll ошибка: {_prescroll_exc}")
        try:
            await page.evaluate("""() => window.scrollTo({ top: 0, left: 0, behavior: 'auto' })""")
        except Exception:
            pass

    # ── CSS pre-scan: строим полную карту hover/pointer элементов до начала движения ──
    # Это позволяет курсору знать заранее куда двигаться, не теряя время на «разведку».
    logger.info("🔍 CSS pre-scan: анализируем hover/transition/pointer элементы страницы...")
    try:
        css_hover_targets = await collect_css_hover_map(page, viewport_width, viewport_height)
        logger.info(f"🔍 CSS pre-scan: найдено {len(css_hover_targets)} потенциальных целей с hover/pointer/transition")
    except Exception:
        css_hover_targets = []

    # ── Header hover pass: показываем hover-эффекты хедера в самом начале записи ──
    try:
        cursor_pos, _header_hovered = await run_header_hover_pass(
            page=page,
            cursor_pos=cursor_pos,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            hover_min_ms=hover_min_ms,
            hover_max_ms=hover_max_ms,
        )
        if _header_hovered > 0:
            logger.info(f"🧭 Header hover pass: показано {_header_hovered} hover-эффектов хедера")
    except Exception:
        pass

    last_analysis_round = -1000
    # ── Быстрые тайминги для «поисковой» фазы (курсор летит к цели) ──
    # Транзитная скорость в 4-6x быстрее человеческой: курсор "прилетает" к цели,
    # а не медленно плывёт, чтобы не терять время на переходы между элементами.
    search_move_min = 8
    search_move_max = 22
    search_dwell_min = 5
    search_dwell_max = 15
    # ── Тайминги для «витрины»: когда эффект обнаружен — показываем по-человечески ──
    micro_hover_min = max(35, int(hover_min_ms * 0.30))
    micro_hover_max = max(micro_hover_min + 15, int(hover_max_ms * 0.45))
    surface_hover_min = max(micro_hover_min + 20, int(hover_min_ms * 0.35))
    surface_hover_max = max(surface_hover_min + 25, int(hover_max_ms * 0.60))
    hover_showcase_min = max(180, int(hover_min_ms * 0.75))
    hover_showcase_max = max(hover_showcase_min + 60, int(hover_max_ms * 0.95))
    # ── Витринные (замедленные) move durations при показе найденного эффекта ──
    showcase_move_surface_min = 100
    showcase_move_surface_max = 260
    showcase_move_text_min = 130
    showcase_move_text_max = 360
    strict_interactions_per_scroll = max(1, min(int(strict_interactions_per_scroll), 4))
    stall_timeout_ms = max(3500, min(int(stall_timeout_ms), 90000))
    rounds_since_scroll = 0
    no_progress_rounds = 0
    interaction_pause_rounds = 0
    last_progress_at = time.monotonic()
    # ── Трекер секции: не более 5 секунд на один экран ──
    # При типичном лендинге (10-15 экранов) это даёт ~50-75с — укладываемся в ~1 минуту.
    section_max_ms = 5000
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

        # ── Universal origin guard: если при любой причине мы сдвинулись с исходной страницы — возвращаемся ──
        _current_loop_url = str(page.url or "")
        _current_loop_path = _url_page_path(_current_loop_url)
        if _current_loop_url and _current_loop_path != pass_origin_path:
            if not is_same_site_url(_current_loop_url, pass_origin_url):
                logger.warning(f"⛔ Origin guard: внешний домен {_current_loop_url}, возвращаемся на {pass_origin_url}")
                await restore_page_location(page, pass_origin_url, timeout=15000)
            else:
                logger.warning(f"⛔ Origin guard: path сдвинулся ({_current_loop_path} ≠ {pass_origin_path}), возвращаемся")
                await restore_page_location(page, pass_origin_url, timeout=15000)
        else:
            await ensure_page_within_allowed_site(
                page,
                site_url,
                fallback_url=pass_origin_url,
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
                # Обогащаем список CSS-предсканом: добавляем цели, которые не попали в DOM-скан.
                # Применяем тот же коммерческий фильтр, что и в основном цикле.
                if css_hover_targets and round_index == 1:
                    existing_keys = {str(t.get("key", "")) for t in analysis_targets}
                    _css_commercial = (
                        "buy", "shop", "cart", "checkout", "pricing", "price",
                        "purchase", "subscribe", "plan", "membership", "donate", "store", "order",
                        "download", "install", "sign up", "signup", "sign in", "signin",
                        "log in", "login", "register", "free trial", "try for free", "try free",
                        "get app", "app store", "google play", "start free", "start trial",
                        "free", "trial", "demo", "try",
                    )
                    def _css_extra_ok(t: Dict[str, Any]) -> bool:
                        t_text = str(t.get("text", "")).strip().lower()
                        t_href = str(t.get("href", "")).strip()
                        # Пропускаем любые элементы с CTA-текстом
                        if t_text and has_keyword(t_text, _css_commercial):
                            return False
                        # Пропускаем элементы без текста, у которых есть навигационная ссылка
                        if not t_text and t_href and is_navigation_like_href(t_href, pass_origin_url):
                            return False
                        return True
                    css_extra = [
                        t for t in css_hover_targets
                        if str(t.get("key", "")) not in existing_keys and _css_extra_ok(t)
                    ]
                    if css_extra:
                        analysis_targets = analysis_targets + css_extra
                        analysis_targets.sort(key=lambda t: (float(t.get("absY", 0)), float(t.get("x", 0))))
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
            pool: Optional[List[Dict[str, Any]]] = None

            candidates: List[Dict[str, Any]] = []
            for item in analysis_targets:
                item_key = str(item.get("key", ""))
                if not item_key or item_key in visited_keys:
                    continue

                item_abs_y = float(item.get("absY", 0.0))
                if not (viewport_top <= int(item_abs_y) <= viewport_bottom):
                    continue
                if bool(item.get("isContentAnchor", False)):
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
                    # standalone "try" — почти всегда CTA «TRY FOR FREE» / «TRY IT»
                    "try",
                )
                # Блокируем элементы с пустым текстом у которых href ведёт на другую страницу
                # (дочерние иконки/стрелки внутри CTA-кнопок)
                if not item_text_low and str(item.get("href", "")).strip():
                    _item_href = str(item.get("href", "")).strip()
                    if is_navigation_like_href(_item_href, current_url):
                        continue
                if has_keyword(item_text_low, _blocked_commercial):
                    continue

                item_family = strict_target_family_key(item)
                if item_family in visited_families:
                    continue
                # ── Проверка чёрного списка навигации: элементы, которые раньше вызвали переход ──
                if is_nav_blacklisted(item, nav_blacklist):
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
                # band_px=70 обеспечивает строгий порядок слева→направо в пределах одной строки экрана.
                # Меньший band = более предсказуемое чтение L→R перед переходом на следующую «строку».
                if surface_hover_candidates:
                    pool = shortlist_progress_targets(surface_hover_candidates, band_px=70.0, limit=min(6, len(surface_hover_candidates)))
                elif hover_text_candidates:
                    pool = shortlist_progress_targets(hover_text_candidates, band_px=70.0, limit=min(6, len(hover_text_candidates)))
                elif any_hover_candidates:
                    pool = shortlist_progress_targets(any_hover_candidates, band_px=80.0, limit=min(6, len(any_hover_candidates)))
                elif inpage_click_enabled and priority_click_candidates:
                    pool = shortlist_progress_targets(priority_click_candidates, band_px=70.0, limit=min(6, len(priority_click_candidates)))
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
                    after_path = _url_page_path(after_url) if after_url else pass_origin_path
                    navigated_away_from_origin = after_path != pass_origin_path
                    navigated_internally = after_url != before_url and is_navigation_like_href(after_url, before_url)
                    if navigated_offsite or navigated_away_from_origin:
                        logger.warning(
                            f"⛔ Smart cursor: уход с исходной страницы ({after_path} ≠ {pass_origin_path}), возвращаемся"
                        )
                        # Добавляем в чёрный список навигации
                        if nav_blacklist is not None:
                            _bl_key = nav_blacklist_key(target)
                            nav_blacklist.add(_bl_key)
                            logger.info(f"🚫 Nav blacklist: добавлен '{_bl_key}' (уход с исходной страницы)")
                        await restore_page_location(page, pass_origin_url, restore_scroll_y=before_scroll, timeout=15000)
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
                        if nav_blacklist is not None:
                            _bl_key = nav_blacklist_key(target)
                            nav_blacklist.add(_bl_key)
                            logger.info(f"🚫 Nav blacklist: добавлен '{_bl_key}' (внутренний переход)")
                        await restore_page_location(page, pass_origin_url, restore_scroll_y=before_scroll, timeout=15000)
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
                            if is_nav_blacklisted(new_item, nav_blacklist):
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
                                if nav_blacklist is not None:
                                    _bl_key = nav_blacklist_key(new_item)
                                    nav_blacklist.add(_bl_key)
                                    logger.info(f"🚫 Nav blacklist: добавлен '{_bl_key}' (внешний deep-click)")
                                await ensure_page_within_allowed_site(
                                    page, site_url, fallback_url=before_url,
                                    fallback_scroll_y=before_scroll, timeout=12000,
                                )
                                break
                            if ni_after_url != before_url and is_navigation_like_href(ni_after_url, before_url) and not allow_internal_nav_click:
                                if nav_blacklist is not None:
                                    _bl_key = nav_blacklist_key(new_item)
                                    nav_blacklist.add(_bl_key)
                                    logger.info(f"🚫 Nav blacklist: добавлен '{_bl_key}' (внутренний deep-click)")
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
                                nav_blacklist=nav_blacklist,
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
                after_metrics = {
                    "scrollY": max(last_scroll_y, scroll_y),
                    "maxScroll": 0,
                    "atBottom": False,
                    "documentHeight": 0,
                    "viewportHeight": viewport_height,
                    "bottomGap": 0,
                }
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
                and not is_probable_top_nav_target(item, viewport_height)
            ),
            default=0,
        )

        bottom_confirmed, bottom_reason = confirm_bottom_state_from_metrics(
            metrics=after_metrics,
            viewport_height=viewport_height,
            analysis_max_abs_y=analysis_max_abs_y,
            round_index=round_index,
            bottom_stable_rounds_required=bottom_stable_rounds_required,
            stagnant_rounds=stagnant_rounds,
        )
        metric_document_height = metrics_document_height(after_metrics, viewport_height)

        if bottom_debug and (
            round_index % 8 == 0
            or (at_bottom and not bottom_confirmed)
            or bottom_confirmed
        ):
            logger.info(
                "🧭 Bottom check: "
                f"atBottom={at_bottom}, confirmed={bottom_confirmed}, reason={bottom_reason}, "
                f"scrollY={current_scroll_y}, maxScroll={max_scroll_y}, docHeight={metric_document_height}, stable={bottom_stable_rounds}"
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
            metric_viewport_height = metrics_viewport_height(after_metrics, viewport_height)
            visible_end = current_scroll_y + metric_viewport_height
            remaining_gap = metric_document_height - visible_end
            if remaining_gap > metric_viewport_height * 0.12:
                logger.info(
                    f"🧭 Smart cursor: atBottom=true, но до конца контента ещё "
                    f"{remaining_gap}px (docH={metric_document_height}, visEnd={visible_end}) — продолжаем"
                )
                bottom_stable_rounds = 0
                stagnant_rounds = 0
                await force_scroll_progress(page, viewport_height)
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
            metrics = {
                "scrollY": last_scroll,
                "maxScroll": 0,
                "atBottom": False,
                "documentHeight": 0,
                "viewportHeight": viewport_height,
                "bottomGap": 0,
            }
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

        stagnant_rounds = 1 if current_scroll <= last_scroll + 2 else 0
        bottom_confirmed, bottom_reason = confirm_bottom_state_from_metrics(
            metrics=metrics,
            viewport_height=viewport_height,
            round_index=round_index,
            bottom_stable_rounds_required=3,
            stagnant_rounds=stagnant_rounds,
        )
        metric_document_height = metrics_document_height(metrics, viewport_height)

        if bottom_debug and (
            round_index % 6 == 0
            or (at_bottom and not bottom_confirmed)
            or bottom_confirmed
        ):
            logger.info(
                "🧭 Force-bottom check: "
                f"atBottom={at_bottom}, confirmed={bottom_confirmed}, reason={bottom_reason}, "
                f"scrollY={current_scroll}, maxScroll={max_scroll}, docHeight={metric_document_height}, stable={stable_rounds}"
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
    nav_blacklist: Optional[Set[str]] = None,
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
                    nav_blacklist=nav_blacklist,
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


def is_scroll_only_heading_landmark(target: Dict[str, Any]) -> bool:
    tag = str(target.get("tag", "")).strip().lower()
    text = str(target.get("text", "")).strip()
    if not text:
        return False
    return tag in {"h1", "h2", "h3", "h4"}


def is_scroll_only_cta_landmark(target: Dict[str, Any], viewport_height: int) -> bool:
    if is_probable_top_nav_target(target, viewport_height):
        return False

    text = str(target.get("text", "")).strip()
    text_low = text.lower()
    if len(text_low) < 3:
        return False

    tag = str(target.get("tag", "")).strip().lower()
    href = str(target.get("href", "")).strip()
    width = max(0.0, _to_float(target.get("width"), 0.0))
    height = max(0.0, _to_float(target.get("height"), 0.0))

    if width < 72 or height < 24:
        return False

    cta_words = (
        "get started", "start", "book", "schedule", "contact", "talk", "demo",
        "pricing", "price", "quote", "request", "learn more", "discover",
        "explore", "join", "subscribe", "sign up", "apply", "buy", "shop",
        "download", "free trial", "trial", "let's talk", "lets talk",
        "try", "try free", "try now", "get it", "order",
        "начать", "попробовать", "купить", "скачать", "подписаться",
        "записаться", "связаться", "заказать", "бронировать",
    )
    button_like = tag in {"button", "a", "summary"} or bool(href)
    compact_label = len(text_low) <= 44 and len(text_low.split()) <= 6

    return button_like and compact_label and has_keyword(text_low, cta_words)


def build_scroll_only_section_landmarks(
    targets: List[Dict[str, Any]],
    viewport_height: int,
) -> List[Dict[str, Any]]:
    """Возвращает только CTA-лендмарки из предварительно собранных целей.
    Заголовки (h1–h4) обнаруживаются в реальном времени во время прокрутки."""
    raw_landmarks: List[Dict[str, Any]] = []

    for item in targets:
        abs_y = max(0.0, _to_float(item.get("absY"), 0.0))
        if abs_y <= 1.0:
            continue

        text = str(item.get("text", "")).strip()
        if not text:
            continue

        if not is_scroll_only_cta_landmark(item, viewport_height):
            continue

        raw_landmarks.append({
            "key": str(item.get("key", "")) or f"cta|{int(abs_y)}|{text[:24]}",
            "absY": abs_y,
            "kind": "cta",
            "label": text[:72],
        })

    raw_landmarks.sort(key=lambda item: float(item.get("absY", 0.0)))

    deduped: List[Dict[str, Any]] = []
    min_gap = max(140.0, viewport_height * 0.18)
    for item in raw_landmarks:
        if not deduped or (float(item.get("absY", 0.0)) - float(deduped[-1].get("absY", 0.0))) >= min_gap:
            deduped.append(item)

    return deduped


# ── JS Hunter smooth scroll controller for Mode 2 ─────────────────────────────
# JS owns linear scrolling and hydration pauses. Python only polls state,
# hovers the returned CTA/heading, and resumes the controller.
_JS_HUNTER_SMOOTH_SCROLL_CONTROLLER = r"""
([action, payload]) => {
    const controllerKey = '__vpvoaeHunterSmoothScrollController';
    const vh = window.innerHeight || document.documentElement?.clientHeight || 0;
    const vw = window.innerWidth || document.documentElement?.clientWidth || 0;
    const focusTopMin = vh * 0.30;
    const focusTopMax = vh * 1.00;
    const focusCenterY = vh * 0.65;
    const CTA_WORDS = [
        'invest', 'get started', 'start free', 'start now', 'free trial', 'trial',
        'book a demo', 'request demo', 'schedule demo', 'contact sales',
        'learn more', 'discover', 'explore', 'pricing', 'book', 'schedule',
        'contact', 'talk', 'demo', 'quote', 'request', 'join', 'subscribe',
        'sign up', 'apply', 'buy', 'shop', 'download', 'try', 'order',
        'view', 'projects', 'services', 'send', 'money', 'our work',
        'open an account', 'open account', 'get account', 'create account',
        'send money', 'receive money', 'transfer money',
        'view case', 'read more', 'case study',
        'начать', 'попробовать', 'купить', 'скачать', 'подписаться',
        'записаться', 'связаться', 'заказать', 'бронировать',
    ];
    const CTA_SKIP_WORDS = [
        'close', 'dismiss', 'cookie', 'accept cookies', 'decline', 'menu',
        'open menu', 'back', 'previous', 'prev', 'next', 'search', 'filter',
        'sort', 'skip to content', 'home', 'language', 'share',
        'закрыть', 'назад', 'меню', 'поиск', 'фильтр',
    ];
    const SECTION_HINT_RE = /hero|cta|pricing|plan|offer|trial|feature|benefit|showcase|product|demo|contact|signup|footer|banner|call[-_ ]?to[-_ ]?action/;

    function normalizeText(text, maxLen) {
        return (text || '').replace(/\s+/g, ' ').trim().slice(0, maxLen);
    }

    function hasKeyword(text, words) {
        const normalized = normalizeText(text, 220).toLowerCase();
        if (!normalized) return false;
        return words.some((word) => normalized.includes(word));
    }

    function longestKeyword(text, words) {
        const normalized = normalizeText(text, 220).toLowerCase();
        if (!normalized) return '';
        let best = '';
        for (const word of words) {
            if (normalized.includes(word) && word.length > best.length) {
                best = word;
            }
        }
        return best;
    }

    function domSignature(el) {
        const parts = [];
        let cur = el;
        let depth = 0;
        while (cur && cur instanceof HTMLElement && depth < 5) {
            const cls = (cur.className || '').toString().trim().split(/\s+/).slice(0, 2).join('.');
            parts.push(`${cur.tagName.toLowerCase()}${cur.id ? '#' + cur.id : ''}${cls ? '.' + cls : ''}`);
            cur = cur.parentElement;
            depth += 1;
        }
        return parts.join('>');
    }

    function textBundle(el, maxLen) {
        return normalizeText([
            el.innerText || '',
            el.textContent || '',
            el.getAttribute('aria-label') || '',
            el.getAttribute('title') || '',
        ].filter(Boolean).join(' '), maxLen);
    }

    function hasFixedOrStickyAncestor(node) {
        let depth = 0;
        while (node && depth < 12) {
            if (!(node instanceof HTMLElement)) break;
            const style = window.getComputedStyle(node);
            if (style.position === 'fixed' || style.position === 'sticky') {
                return true;
            }
            node = node.parentElement;
            depth += 1;
        }
        return false;
    }

    function hasRenderableBox(el, rect, style) {
        if (!(el instanceof HTMLElement)) return false;
        const rects = typeof el.getClientRects === 'function' ? el.getClientRects() : [];
        if (!rects || rects.length === 0) return false;
        if (!rect || rect.width <= 0 || rect.height <= 0) return false;
        if (rect.left < 0 || rect.top < 0) return false;
        if (rect.right > vw || rect.bottom > vh) return false;
        if (style.display === 'none' || style.visibility !== 'visible') return false;
        if (Number(style.opacity || '1') <= 0.5) return false;
        if (style.pointerEvents === 'none') return false;
        return true;
    }

    function isViewportContent(el) {
        if (!(el instanceof HTMLElement)) return false;
        const style = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        if (!hasRenderableBox(el, rect, style)) return false;
        if (style.position === 'fixed' || style.position === 'sticky') return false;
        if (hasFixedOrStickyAncestor(el.parentElement)) return false;
        return true;
    }

    function isInFocusBand(rect) {
        const centerY = rect.top + rect.height / 2;
        return Number.isFinite(centerY) && centerY >= focusTopMin && centerY <= focusTopMax;
    }

    function centerPoint(rect) {
        return {
            x: rect.left + rect.width / 2,
            y: rect.top + rect.height / 2,
        };
    }

    function scoreByCenter(rect) {
        const center = centerPoint(rect);
        return Math.max(0, 220 - Math.abs(center.y - focusCenterY) * 1.15);
    }

    function parseRgb(raw) {
        const match = (raw || '').match(/\d+(?:\.\d+)?/g);
        if (!match || match.length < 3) return null;
        return match.slice(0, 3).map((value) => Number(value));
    }

    function getLuminance(raw) {
        const channels = parseRgb(raw);
        if (!channels) return null;
        const [r, g, b] = channels.map((channel) => {
            const normalized = channel / 255;
            return normalized <= 0.03928
                ? normalized / 12.92
                : Math.pow((normalized + 0.055) / 1.055, 2.4);
        });
        return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    }

    function contrastRatio(colorA, colorB) {
        const lumA = getLuminance(colorA);
        const lumB = getLuminance(colorB);
        if (lumA === null || lumB === null) return 1.0;
        const bright = Math.max(lumA, lumB);
        const dark = Math.min(lumA, lumB);
        return (bright + 0.05) / (dark + 0.05);
    }

    function firstSolidBackground(node) {
        let cur = node;
        let depth = 0;
        while (cur && cur instanceof HTMLElement && depth < 8) {
            const bg = (window.getComputedStyle(cur).backgroundColor || '').toLowerCase();
            if (bg && bg !== 'transparent' && bg !== 'rgba(0, 0, 0, 0)') {
                return bg;
            }
            cur = cur.parentElement;
            depth += 1;
        }
        return 'rgb(255, 255, 255)';
    }

    function hasVisibleFill(style) {
        const bg = (style.backgroundColor || '').toLowerCase();
        const borderWidth = Number.parseFloat(style.borderTopWidth || '0') || 0;
        return (
            (bg && bg !== 'transparent' && bg !== 'rgba(0, 0, 0, 0)')
            || borderWidth >= 1.0
        );
    }

    function findSectionContainer(el) {
        let best = null;
        let node = el;
        let depth = 0;
        while (node && node instanceof HTMLElement && depth < 8) {
            const style = window.getComputedStyle(node);
            if (style.position !== 'fixed' && style.position !== 'sticky') {
                const rect = node.getBoundingClientRect();
                const tag = node.tagName.toLowerCase();
                const hint = `${tag} ${(node.id || '').toString().toLowerCase()} ${(node.className || '').toString().toLowerCase()}`;
                const semantic = ['section', 'article', 'main', 'aside', 'form', 'footer'].includes(tag);
                const hinted = SECTION_HINT_RE.test(hint);
                const largeBlock = rect.width >= vw * 0.48 && rect.height >= Math.max(140, vh * 0.24);
                if ((semantic || hinted || largeBlock) && rect.width >= 120 && rect.height >= 80) {
                    best = node;
                    if (semantic || hinted) break;
                }
            }
            node = node.parentElement;
            depth += 1;
        }
        return best;
    }

    function sectionSignature(sectionEl) {
        if (!(sectionEl instanceof HTMLElement)) return '';
        const rect = sectionEl.getBoundingClientRect();
        const top = Math.round(rect.top + getScrollY());
        const height = Math.round(rect.height);
        const label = normalizeText(textBundle(sectionEl, 48), 48);
        return ['section', domSignature(sectionEl), top, height, label].join('|');
    }

    function findSectionHeading(sectionEl, anchorRect) {
        if (!(sectionEl instanceof HTMLElement)) return '';
        let bestText = '';
        let bestScore = -Infinity;
        for (const node of sectionEl.querySelectorAll('h1,h2,h3,h4')) {
            if (!(node instanceof HTMLElement)) continue;
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            if (!hasRenderableBox(node, rect, style)) continue;
            if (rect.bottom < -40 || rect.top > vh * 0.78) continue;
            const text = normalizeText(node.innerText || node.textContent || '', 120);
            if (!text) continue;
            const verticalDelta = anchorRect.top - rect.top;
            if (verticalDelta < -140 || verticalDelta > vh * 0.72) continue;
            const horizontalDelta = Math.abs(
                (rect.left + rect.width / 2) - (anchorRect.left + anchorRect.width / 2)
            );
            const score = 260 - Math.abs(verticalDelta - Math.min(140, vh * 0.16)) - horizontalDelta * 0.18;
            if (score > bestScore) {
                bestScore = score;
                bestText = text;
            }
        }
        return bestText;
    }

    function buildStableKey(el, kind, text, rect, sectionKey = '') {
        return [
            kind,
            sectionKey || domSignature(el),
            normalizeText(text, 36),
            Math.round(rect.left + rect.width / 2),
            Math.round(rect.top + rect.height / 2),
        ].join('|');
    }

    function markSeenElement(el, key) {
        if (!(el instanceof HTMLElement) || !key) return;
        try {
            el.dataset.seen = 'true';
            el.dataset.vpvoaeSeen = 'true';
            el.dataset.vpvoaeKey = key;
        } catch (e) {}
    }

    // Detect the real scroll container (for Wise, Revolut, etc. that use overflow:hidden on body)
    let _cachedScrollContainer = null;
    let _containerCheckAt = 0;
    function findScrollContainer() {
        const now = Date.now();
        if (_cachedScrollContainer && (now - _containerCheckAt) < 3000) {
            // Verify it's still valid
            if (_cachedScrollContainer.scrollHeight > _cachedScrollContainer.clientHeight + 20) {
                return _cachedScrollContainer;
            }
            _cachedScrollContainer = null;
        }
        _containerCheckAt = now;
        // Check common SPA wrappers first
        for (const sel of ['#__next', '#root', '#app', 'main', '[data-scroll-container]', '.page-wrapper', '.main-wrapper', '.site-wrapper']) {
            try {
                const el = document.querySelector(sel);
                if (el && el.scrollHeight > el.clientHeight + 20) {
                    const s = window.getComputedStyle(el);
                    const ov = s.overflowY || '';
                    if (ov === 'auto' || ov === 'scroll') {
                        _cachedScrollContainer = el;
                        return el;
                    }
                }
            } catch(e) {}
        }
        // Walk top-level children of body — find the largest scrollable one
        try {
            let best = null;
            let bestHeight = 0;
            for (const el of document.body.children) {
                if (!(el instanceof HTMLElement)) continue;
                if (el.scrollHeight <= el.clientHeight + 20) continue;
                const s = window.getComputedStyle(el);
                const ov = s.overflowY || '';
                if (ov !== 'auto' && ov !== 'scroll') continue;
                if (el.scrollHeight > bestHeight) {
                    bestHeight = el.scrollHeight;
                    best = el;
                }
            }
            if (best) {
                _cachedScrollContainer = best;
                return best;
            }
        } catch(e) {}
        return null;
    }

    function getScrollY() {
        const container = findScrollContainer();
        return Math.round(Math.max(
            Number(window.scrollY || window.pageYOffset || 0),
            Number(document.documentElement?.scrollTop || 0),
            Number(document.body?.scrollTop || 0),
            container ? Number(container.scrollTop || 0) : 0,
        ));
    }

    function getDocumentHeight() {
        const container = findScrollContainer();
        return Math.round(Math.max(
            document.body?.scrollHeight || 0,
            document.documentElement?.scrollHeight || 0,
            container ? (container.scrollHeight || 0) : 0,
            getScrollY() + vh,
        ));
    }

    function buildPayload(candidate, kind) {
        if (!candidate || !(candidate.el instanceof HTMLElement)) return null;
        const rects = typeof candidate.el.getClientRects === 'function' ? candidate.el.getClientRects() : [];
        if (!rects || rects.length === 0) return null;
        const center = centerPoint(candidate.rect);
        return {
            kind,
            text: candidate.text,
            dedupKey: buildStableKey(candidate.el, kind, candidate.text, candidate.rect, candidate.sectionKey || ''),
            sectionKey: candidate.sectionKey || '',
            headingText: candidate.headingText || '',
            x: Math.round(Math.max(2, Math.min(vw - 2, center.x))),
            y: Math.round(Math.max(2, Math.min(vh - 2, center.y))),
            top: Math.round(candidate.rect.top),
            score: Math.round(candidate.score),
        };
    }

    function findBestTarget(seenKeys, seenSectionKeys, currentScrollY, lastFocusScrollY, focusMinGapPx) {
        if (currentScrollY < (lastFocusScrollY + focusMinGapPx)) {
            return null;
        }

        const ctaSelectors = [
            'button', 'a[href]', '[role="button"]',
            '[class*="btn"]', '[class*="cta"]', '[class*="action"]',
            '[data-cta]', '.btn', '.button', '[type="submit"]',
        ];
        const ctaPool = new Set();
        for (const selector of ctaSelectors) {
            try {
                for (const el of document.querySelectorAll(selector)) {
                    if (el instanceof HTMLElement) ctaPool.add(el);
                }
            } catch (e) {}
        }

        let bestCta = null;
        for (const el of ctaPool) {
            if (!(el instanceof HTMLElement)) continue;
            if (el.dataset.seen === 'true') continue;  // already processed in queue
            const style = window.getComputedStyle(el);
            if (style.position === 'fixed' || style.position === 'sticky') continue;
            if (hasFixedOrStickyAncestor(el.parentElement)) continue;
            const rect = el.getBoundingClientRect();
            // Relaxed visibility: only require a rendered box (height > 0), skip opacity check
            if (!rect || rect.width <= 0 || rect.height <= 0) continue;
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            if (style.pointerEvents === 'none') continue;
            if (!isInFocusBand(rect)) continue;
            if (rect.width < 72 || rect.height < 26) continue;

            const text = textBundle(el, 84);
            if (!text) continue;
            const textLow = text.toLowerCase();
            if (hasKeyword(textLow, CTA_SKIP_WORDS)) continue;

            const hrefLow = (el.getAttribute('href') || '').toLowerCase();
            const keyword = longestKeyword(`${textLow} ${hrefLow}`, CTA_WORDS);
            if (!keyword) continue;

            const area = rect.width * rect.height;
            const hasFill = hasVisibleFill(style);
            if (area < 500) continue;  // ignore tiny icons
            if (area < 2400 && !hasFill) continue;

            const sectionEl = findSectionContainer(el);
            const sectionKey = sectionEl ? sectionSignature(sectionEl) : '';
            if (sectionKey && seenSectionKeys.has(sectionKey)) continue;

            const dedupKey = buildStableKey(el, 'cta', text, rect, sectionKey);
            if (seenKeys.has(dedupKey)) continue;

            const headingScope = (sectionEl instanceof HTMLElement) ? sectionEl : el.parentElement;
            const headingText = findSectionHeading(headingScope, rect);
            const contrast = contrastRatio(
                style.backgroundColor || '',
                firstSolidBackground((sectionEl && sectionEl !== el) ? sectionEl : el.parentElement),
            );
            const sectionRect = sectionEl instanceof HTMLElement ? sectionEl.getBoundingClientRect() : rect;
            const keywordBoost = 190 + keyword.length * 6;
            const areaBoost = Math.min(area, 64000) * 0.0022;
            const fillBoost = hasFill ? 75 : 0;
            const contrastBoost = contrast >= 2.4 ? 120 : contrast >= 1.8 ? 65 : contrast >= 1.4 ? 20 : -55;
            const headingBoost = headingText ? 110 + Math.min(headingText.length, 64) * 0.65 : 0;
            const sectionBoost = Math.min(Math.max(sectionRect.height, rect.height), vh * 1.4) * 0.05;
            const shapeBoost = rect.width >= 132 ? 38 : 0;
            const edgePenalty = (rect.left <= 18 || rect.right >= vw - 18) ? 35 : 0;
            const inlinePenalty = (!hasFill && rect.height < 34) ? 90 : 0;
            const score = (
                keywordBoost
                + areaBoost
                + fillBoost
                + contrastBoost
                + headingBoost
                + sectionBoost
                + shapeBoost
                + scoreByCenter(rect)
                - edgePenalty
                - inlinePenalty
            );

            if (score < 340) continue;

            if (!bestCta || score > bestCta.score) {
                bestCta = { el, rect, text, score, sectionKey, headingText };
            }
        }

        const ctaPayload = buildPayload(bestCta, 'cta');
        if (ctaPayload) return { payload: ctaPayload, element: bestCta.el };
        return null;
    }

    function nowMs() {
        return window.performance && typeof window.performance.now === 'function'
            ? window.performance.now()
            : Date.now();
    }

    function getDomHash() {
        try { return document.querySelectorAll('*').length; } catch(e) { return -1; }
    }

    function isPageLoading() {
        try {
            const bodyStyle = window.getComputedStyle(document.body);
            if (parseFloat(bodyStyle.opacity || '1') < 0.05) return true;
            for (const el of Array.from(document.body.children)) {
                if (!(el instanceof HTMLElement)) continue;
                const s = window.getComputedStyle(el);
                if (s.display === 'none' || s.visibility === 'hidden') continue;
                if (s.position !== 'fixed' && s.position !== 'absolute') continue;
                if (parseFloat(s.opacity || '1') < 0.5) continue;
                const r = el.getBoundingClientRect();
                if (r.width >= vw * 0.9 && r.height >= vh * 0.9) return true;
            }
        } catch(e) {}
        return false;
    }

    function createController() {
        const state = {
            running: false,
            finished: false,
            pausedForFocus: false,
            pausedForHydration: false,
            focusTarget: null,
            focusElement: null,
            seenKeys: new Set(),
            seenSectionKeys: new Set(),
            rafId: 0,
            pauseUntil: 0,
            basePxPerFrame: 8.0,
            slowPxPerFrame: 5.6,
            hydrationDistancePx: 2800,
            hydrationPauseMs: 325,
            nextHydrationPauseAt: 0,
            focusMinGapPx: 0,
            slowdownDistancePx: 0,
            slowUntilScrollY: 0,
            lastFocusScrollY: -100000,
            stagnantScrollRounds: 0,
            stagnantDomRounds: 0,
            lastDomHash: -1,
            virtualScrollProgress: 0,
            lastVirtualProgress: 0,
            lockedFrames: 0,
            lockedTarget: null,
            lockedElement: null,
            noContentSinceScrollY: 0,
            atBottomSince: 0,
            lastSensorAt: 0,
            lastLifeAt: 0,
            lastProgressAt: 0,
            lastScrollHeight: 0,
            lastObservedScrollY: 0,
            startedAt: 0,
            stallTimeoutMs: 5000,
            reason: 'idle',
        };

        function snapshot() {
            const currentScrollY = getScrollY();
            const bodyScrollHeight = getDocumentHeight();
            return {
                running: state.running,
                finished: state.finished,
                pausedForFocus: state.pausedForFocus,
                pausedForHydration: state.pausedForHydration,
                focusTarget: state.focusTarget,
                currentScrollY,
                scrollY: currentScrollY,
                documentHeight: bodyScrollHeight,
                bodyScrollHeight,
                reason: state.reason,
                elapsedMs: Math.max(0, Math.round(nowMs() - state.startedAt)),
                isLoading: isPageLoading(),
                virtualScrollProgress: state.virtualScrollProgress,
                stagnantDomRounds: state.stagnantDomRounds,
                noContentSinceScrollY: state.noContentSinceScrollY,
            };
        }

        function clearFrame() {
            if (state.rafId) {
                window.cancelAnimationFrame(state.rafId);
                state.rafId = 0;
            }
        }

        function finish(reason) {
            state.running = false;
            state.finished = true;
            state.pausedForFocus = false;
            state.pausedForHydration = false;
            state.focusTarget = null;
            state.focusElement = null;
            state.reason = reason;
            clearFrame();
            return snapshot();
        }

        function schedule() {
            if (state.rafId || !state.running || state.finished || state.pausedForFocus) {
                return;
            }
            state.rafId = window.requestAnimationFrame(tick);
        }

        function hasContentHeadingInView() {
            try {
                for (const el of document.querySelectorAll('h1,h2')) {
                    if (!(el instanceof HTMLElement)) continue;
                    const r = el.getBoundingClientRect();
                    if (r.top >= vh * 0.1 && r.bottom <= vh * 0.95 && r.width > 40 && r.height > 10) return true;
                }
            } catch(e) {}
            return false;
        }

        function scanForLife(timestamp) {
            const currentHeight = getDocumentHeight();
            const currentScrollY = getScrollY();
            if (currentHeight !== state.lastScrollHeight) {
                state.lastScrollHeight = currentHeight;
                state.lastLifeAt = timestamp;
                state.atBottomSince = 0;  // page grew — reset bottom grace timer
            }
            if (currentScrollY >= state.lastObservedScrollY + 40) {
                state.lastObservedScrollY = currentScrollY;
                state.lastProgressAt = timestamp;
                state.stagnantScrollRounds = 0;
            } else {
                state.stagnantScrollRounds += 1;
            }

            // Also treat virtual-scroll advancement as real progress (for custom-container sites)
            if (state.virtualScrollProgress > state.lastVirtualProgress + 30) {
                state.lastVirtualProgress = state.virtualScrollProgress;
                state.lastProgressAt = timestamp;
            }

            // Content stall: if no CTA/h1-h2 in last 3000px of virtual scroll, finish early
            const distSinceContent = state.virtualScrollProgress - state.noContentSinceScrollY;
            const hasHeading = hasContentHeadingInView();
            // We check findBestTarget below — update noContentSinceScrollY if something found

            const currentDomHash = getDomHash();
            if (currentDomHash === state.lastDomHash) {
                state.stagnantDomRounds += 1;
            } else {
                state.stagnantDomRounds = 0;
                state.lastDomHash = currentDomHash;
            }

            const candidate = findBestTarget(
                state.seenKeys,
                state.seenSectionKeys,
                currentScrollY,
                state.lastFocusScrollY,
                state.focusMinGapPx,
            );
            if (candidate && candidate.payload) {
                state.noContentSinceScrollY = state.virtualScrollProgress;
                // Queue: mark element as seen immediately so it won't be picked again
                if (candidate.element instanceof HTMLElement) {
                    candidate.element.dataset.seen = 'true';
                }
                // Don't pause immediately — lock and let tick() centre the element first
                state.lockedTarget = candidate.payload;
                state.lockedElement = candidate.element || null;
                state.lastLifeAt = timestamp;
                return;
            }

            if (hasHeading) {
                state.noContentSinceScrollY = state.virtualScrollProgress;
                state.lastLifeAt = timestamp;
            } else if (distSinceContent > 12000) {
                // 12000px of virtual scroll with no CTA or h1/h2 = deep footer zone, stop early
                finish('bottom');
                return;
            }

            // Python owns virtual-progress termination via virtualScrollProgress field.

            if ((timestamp - Math.max(state.lastLifeAt, state.lastProgressAt)) >= state.stallTimeoutMs) {
                finish('idle');
            }
        }

        // tick() is now a pure CTA radar — Python drives actual scrolling via mouse.wheel
        function tick(timestamp) {
            state.rafId = 0;
            if (!state.running || state.finished || state.pausedForFocus) {
                return;
            }

            // Advance virtual odometer (Python sends wheel events, we just track)
            state.virtualScrollProgress += 6;

            // Run CTA sensor every second
            if (!state.lastSensorAt || (timestamp - state.lastSensorAt) >= 1000) {
                state.lastSensorAt = timestamp;
                scanForLife(timestamp);
                if (!state.running || state.finished || state.pausedForFocus) {
                    return;
                }
            }

            // Target locking: if we have a locked CTA, check if it reached center
            if (state.lockedTarget && state.lockedElement instanceof HTMLElement) {
                state.lockedFrames = (state.lockedFrames || 0) + 1;
                const lockedRect = state.lockedElement.getBoundingClientRect();
                const topRatio = lockedRect.top / vh;
                if (
                    lockedRect.width > 0 && lockedRect.height > 0 &&
                    topRatio >= 0.30 && topRatio <= 0.65
                ) {
                    state.focusTarget = state.lockedTarget;
                    state.focusElement = state.lockedElement;
                    state.lockedTarget = null;
                    state.lockedElement = null;
                    state.lockedFrames = 0;
                    state.pausedForFocus = true;
                    state.running = false;
                    state.reason = 'focus';
                    clearFrame();
                    return;
                }
                if (state.lockedFrames > 180) {
                    state.lockedTarget = null;
                    state.lockedElement = null;
                    state.lockedFrames = 0;
                }
            }

            // Check for bottom (native scroll sites only)
            const currentScrollY = getScrollY();
            const bodyScrollHeight = getDocumentHeight();
            const maxScrollY = Math.max(0, bodyScrollHeight - vh);
            if (maxScrollY > 2 && currentScrollY >= maxScrollY - 2) {
                if (!state.atBottomSince) state.atBottomSince = timestamp;
                if (timestamp - state.atBottomSince >= 3000) {
                    finish('bottom');
                    return;
                }
            } else {
                state.atBottomSince = 0;
            }

            schedule();
        }

        return {
            startSmoothScroll(config) {
                clearFrame();
                const timestamp = nowMs();
                state.running = true;
                state.finished = false;
                state.pausedForFocus = false;
                state.pausedForHydration = false;
                state.focusTarget = null;
                state.focusElement = null;
                state.seenKeys = new Set();
                state.seenSectionKeys = new Set();
                state.reason = 'running';
                state.basePxPerFrame = Math.max(6.0, Math.min(10.0, Number(config && config.pxPerFrame) || 8.0));
                const slowdownFactor = Math.max(0.45, Math.min(1.0, Number(config && config.slowdownFactor) || 0.74));
                state.slowPxPerFrame = Math.max(1.1, Math.min(state.basePxPerFrame, state.basePxPerFrame * slowdownFactor));
                state.hydrationDistancePx = Math.max(1200, Math.round(Number(config && config.hydrationDistancePx) || 2800));
                state.hydrationPauseMs = Math.max(90, Math.round(Number(config && config.hydrationPauseMs) || 325));
                state.focusMinGapPx = Math.max(Math.round(vh * 0.75), Math.round(Number(config && config.focusMinGapPx) || (vh * 0.95)));
                state.slowdownDistancePx = Math.max(0, Math.round(Number(config && config.slowdownDistancePx) || (vh * 0.8)));
                state.slowUntilScrollY = 0;
                state.lastFocusScrollY = -state.focusMinGapPx;
                state.pauseUntil = 0;
                state.lastSensorAt = 0;
                state.lastLifeAt = timestamp;
                state.lastProgressAt = timestamp;
                state.startedAt = timestamp;
                state.lastScrollHeight = getDocumentHeight();
                state.lastObservedScrollY = getScrollY();
                state.stagnantScrollRounds = 0;
                state.stagnantDomRounds = 0;
                state.lastDomHash = getDomHash();
                state.lockedFrames = 0;
                state.wheelFrameCounter = 0;
                state.stallTimeoutMs = Math.max(5000, Math.round(Number(config && config.stallTimeoutMs) || 10000));
                state.nextHydrationPauseAt = getScrollY() + state.hydrationDistancePx;
                schedule();
                return snapshot();
            },

            readState() {
                return snapshot();
            },

            resumeAfterFocus(resumePayload) {
                const timestamp = nowMs();
                const focusKey = String(
                    (resumePayload && resumePayload.dedupKey) ||
                    (state.focusTarget && state.focusTarget.dedupKey) ||
                    ''
                );
                const sectionKey = String(
                    (resumePayload && resumePayload.sectionKey) ||
                    (state.focusTarget && state.focusTarget.sectionKey) ||
                    ''
                );
                if (focusKey) {
                    state.seenKeys.add(focusKey);
                    if (state.focusElement instanceof HTMLElement) {
                        markSeenElement(state.focusElement, focusKey);
                    }
                }
                if (sectionKey) {
                    state.seenSectionKeys.add(sectionKey);
                }
                state.focusTarget = null;
                state.focusElement = null;
                state.finished = false;
                state.pausedForFocus = false;
                state.running = true;
                state.reason = 'running';
                state.lastLifeAt = timestamp;
                state.lastProgressAt = timestamp;
                state.lastSensorAt = 0;
                state.lastObservedScrollY = getScrollY();
                state.lastFocusScrollY = state.lastObservedScrollY;
                state.lockedTarget = null;
                state.lockedElement = null;
                state.stagnantScrollRounds = 0;
                state.stagnantDomRounds = 0;
                if (state.slowdownDistancePx > 0 && state.slowPxPerFrame < state.basePxPerFrame) {
                    state.slowUntilScrollY = Math.max(
                        state.slowUntilScrollY,
                        state.lastFocusScrollY + state.slowdownDistancePx,
                    );
                }
                schedule();
                return snapshot();
            },

            stopSmoothScroll() {
                clearFrame();
                state.running = false;
                state.finished = false;
                state.pausedForFocus = false;
                state.pausedForHydration = false;
                state.focusTarget = null;
                state.focusElement = null;
                state.reason = 'stopped';
                return snapshot();
            },
        };
    }

    if (!window[controllerKey]) {
        window[controllerKey] = createController();
    }

    const controller = window[controllerKey];
    if (action === 'start') return controller.startSmoothScroll(payload || {});
    if (action === 'resume') return controller.resumeAfterFocus(payload || {});
    if (action === 'stop') return controller.stopSmoothScroll();
    if (action === 'state') return controller.readState();
    return controller.readState();
}"""


async def run_scroll_only_down_pass(
    page: Any,
    viewport_width: int,
    viewport_height: int,
    total_time_ms: int,
    bottom_stable_rounds_required: int,
    scroll_speed_factor: float,
    scroll_pause_min_ms: int,
    scroll_pause_max_ms: int,
    scroll_finish_timeout_ms: int,
    require_bottom: bool,
    require_bottom_max_ms: int,
    bottom_debug: bool,
    stall_timeout_ms: int,
    cursor_pos: Tuple[float, float] = (960.0, 540.0),
) -> Tuple[bool, Tuple[float, float]]:
    """CTA-Analyzer mode: autonomous JS smooth scroll with Python CTA hovers."""
    reached_bottom = False
    section_pause_ms = max(225, min(env_int("SMART_CURSOR_SCROLL_ONLY_SECTION_PAUSE_MS", 550), 2000))
    section_slowdown_factor = clamp(env_float("SMART_CURSOR_SCROLL_ONLY_SECTION_SLOWDOWN_FACTOR", 0.74), 0.45, 1.0)
    section_slowdown_rounds = max(0, min(env_int("SMART_CURSOR_SCROLL_ONLY_SECTION_SLOWDOWN_ROUNDS", 1), 4))
    base_speed_factor = clamp(scroll_speed_factor if math.isfinite(scroll_speed_factor) else 1.0, 0.80, 2.40)
    px_per_frame = clamp(8.0 * base_speed_factor, 6.0, 10.0)
    hydration_distance_px = max(1600, int(viewport_height * 2.4 * clamp(base_speed_factor, 0.9, 1.7)))
    hydration_pause_ms = max(130, min(int(320 / clamp(base_speed_factor, 0.9, 2.0)), 475))
    focus_min_gap_px = max(int(viewport_height * 0.90), int(viewport_height * (0.72 + 0.16 * section_slowdown_rounds)))
    slowdown_distance_px = int(viewport_height * 0.78 * section_slowdown_rounds)
    hover_pause_ms = section_pause_ms
    poll_interval_s = 0.22
    scroll_config = {
        "pxPerFrame": px_per_frame,
        "hydrationDistancePx": hydration_distance_px,
        "hydrationPauseMs": hydration_pause_ms,
        "slowdownFactor": section_slowdown_factor,
        "focusMinGapPx": focus_min_gap_px,
        "slowdownDistancePx": slowdown_distance_px,
        "stallTimeoutMs": max(2600, stall_timeout_ms),
    }
    started_at = time.monotonic()
    hard_deadline = started_at + ((total_time_ms / 1000.0) if total_time_ms > 0 else 300.0)
    controller_started = False

    try:
        await page.evaluate("""() => window.scrollTo({ top: 0, left: 0, behavior: 'auto' })""")
        await page.wait_for_timeout(random.randint(120, 260))
    except Exception:
        pass

    async def controller_call(action: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            result = await page.evaluate(_JS_HUNTER_SMOOTH_SCROLL_CONTROLLER, [action, payload or {}])
        except Exception as exc:
            if _is_nav_error(exc):
                await _recover_after_nav(page)
                result = await page.evaluate(_JS_HUNTER_SMOOTH_SCROLL_CONTROLLER, [action, payload or {}])
            else:
                raise
        return result if isinstance(result, dict) else {}

    # Принудительный фокус: клик по центру страницы для передачи фокуса
    try:
        await page.mouse.click(viewport_width // 2, viewport_height // 2)
        await asyncio.sleep(0.3)
        # Оставляем мышь в центре — wheel будет идти отсюда
        await page.mouse.move(viewport_width // 2, viewport_height // 2)
    except Exception:
        pass

    try:
        await controller_call("start", scroll_config)
        controller_started = True
        logger.info(
            f"[INFO] Scroll-only: Native Wheel Engine запущен (mouse.wheel driven, JS radar only)."
        )

        last_logged_scroll_y: Optional[int] = None
        last_phase = ""
        MAX_VIRTUAL_DISTANCE = 15_000
        loop_start_time = time.monotonic()
        START_IMMUNITY_SEC = 20.0  # запрещено прерывать цикл первые 20 секунд
        wheel_iteration = 0
        WHEEL_STEP = 32  # px — между тачпадом и колёсиком, достаточно для scroll-snap порогов
        WHEEL_SLEEP = 0.050  # ~20 итераций/сек ≈ 640px/сек — плавно на 30fps видео
        JS_SCAN_EVERY = 15  # опрашивать JS-радар каждые N итераций (~0.75с)
        prev_scroll_y = 0
        stall_counter = 0

        while True:
            now = time.monotonic()
            _elapsed = now - loop_start_time
            is_immune = _elapsed < START_IMMUNITY_SEC

            if now >= hard_deadline:
                logger.warning("[WARN] Scroll-only: превышен общий таймаут.")
                break

            # Нативный скролл через колёсико мыши (мышь уже в центре)
            try:
                await page.mouse.wheel(0, WHEEL_STEP)
            except Exception:
                pass
            await asyncio.sleep(WHEEL_SLEEP)
            wheel_iteration += 1

            # Опрашиваем JS-радар каждые JS_SCAN_EVERY итераций (~0.75с)
            if wheel_iteration % JS_SCAN_EVERY != 0:
                continue

            state = await controller_call("state")
            current_scroll_y = max(0, int(state.get("currentScrollY", state.get("scrollY", 0))))
            document_height = max(viewport_height, int(state.get("documentHeight", viewport_height)))
            body_scroll_height = max(viewport_height, int(state.get("bodyScrollHeight", document_height)))
            finished = bool(state.get("finished", False))
            paused_for_focus = bool(state.get("pausedForFocus", False))
            reason = str(state.get("reason", "")).strip() or "running"
            raw_focus_target = state.get("focusTarget")
            focus_target = raw_focus_target if isinstance(raw_focus_target, dict) else None
            virtual_progress = int(state.get("virtualScrollProgress", 0))
            stagnant_dom_rounds = int(state.get("stagnantDomRounds", 0))
            no_content_since = int(state.get("noContentSinceScrollY", 0))
            dist_since_content = virtual_progress - no_content_since

            # Детектор застоя по реальной позиции скролла
            if current_scroll_y <= prev_scroll_y + 5:
                stall_counter += 1
            else:
                stall_counter = 0
                prev_scroll_y = current_scroll_y

            phase = "finished" if finished else "focus" if paused_for_focus else "scrolling"
            if phase != last_phase or last_logged_scroll_y is None or abs(current_scroll_y - last_logged_scroll_y) >= 300:
                logger.info(
                    f"[Wheel] phase={phase} | scrollY={current_scroll_y} | virtual={virtual_progress}/{MAX_VIRTUAL_DISTANCE} "
                    f"| stall={stall_counter} | domStagnant={stagnant_dom_rounds} "
                    f"| bodyHeight={body_scroll_height} | elapsed={_elapsed:.1f}s | reason={reason}"
                )
                last_phase = phase
                last_logged_scroll_y = current_scroll_y

            # Завершение — но не в период иммунитета
            if finished and not is_immune:
                logger.info("[INFO] Scroll-only: JS сообщил finished=true, завершаем проход.")
                reached_bottom = True
                break

            if virtual_progress >= MAX_VIRTUAL_DISTANCE and not is_immune:
                logger.info(
                    f"[Scroll-only] virtual={virtual_progress} >= {MAX_VIRTUAL_DISTANCE} — конец, завершаем."
                )
                reached_bottom = True
                break

            # Застой: 25 опросов без движения (×10 итераций × 0.04с = ~10с)
            if stall_counter >= 25 and not is_immune:
                logger.info(
                    f"[Wheel] scrollY не менялся {stall_counter} раундов, завершаем."
                )
                reached_bottom = True
                break

            # CTA найден — пауза + hover
            if paused_for_focus:
                if not focus_target:
                    logger.warning("[WARN] JS пауза без focusTarget, продолжаем.")
                    await controller_call("resume", {})
                    continue

                focus_x = clamp(float(focus_target.get("x", viewport_width * 0.5)), 2, viewport_width - 2)
                focus_y = clamp(float(focus_target.get("y", viewport_height * 0.5)), 2, viewport_height - 2)
                focus_text = str(focus_target.get("text", "")).strip()
                focus_kind = str(focus_target.get("kind", "object")).strip() or "object"
                focus_key = str(focus_target.get("dedupKey", "")).strip()
                focus_heading = str(focus_target.get("headingText", "")).strip()
                if focus_heading:
                    logger.info(
                        f"🎯 CTA '{focus_text[:30]}' в секции '{focus_heading[:36]}', hover."
                    )
                else:
                    logger.info(f"🎯 CTA '{focus_text[:30]}', hover.")
                try:
                    await page.mouse.move(focus_x, focus_y, steps=20)
                    cursor_pos = (focus_x, focus_y)
                    await asyncio.sleep(1.2)
                except Exception as focus_exc:
                    if _is_nav_error(focus_exc):
                        await _recover_after_nav(page)
                    else:
                        raise

                # Safe-zone reset перед возобновлением
                try:
                    await page.mouse.move(50, 50, steps=5)
                except Exception:
                    pass

                await controller_call(
                    "resume",
                    {
                        "dedupKey": focus_key,
                        "sectionKey": str(focus_target.get("sectionKey", "")).strip(),
                    },
                )
                logger.info("[INFO] Wheel: скролл возобновлен после hover.")
                continue

            # Никаких пауз.  Продолжаем крутить.
    finally:
        if controller_started:
            try:
                await controller_call("stop")
            except Exception:
                pass

    return reached_bottom, cursor_pos


async def run_smart_cursor(
    page: Any,
    site_url: str,
    viewport_width: int,
    viewport_height: int,
    total_time_ms: int,
    max_targets: int,
    hover_min_ms: int,
    hover_max_ms: int,
    smart_cursor_mode: str,
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
    nav_blacklist: Set[str] = set()
    scroll_only_mode_active = smart_cursor_mode == "scroll_only"

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
    if entry_click_enabled and not scroll_only_mode_active:
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

    # Закрытие cookie-баннеров (универсально для всех режимов)
    try:
        cursor_pos, cookie_closed = await try_close_cookie_banner(
            page, cursor_pos, viewport_width, viewport_height,
        )
        if cookie_closed:
            await page.wait_for_timeout(random.randint(300, 600))
    except Exception as exc:
        if _is_nav_error(exc):
            await _recover_after_nav(page)

    # Скрытие чат-виджетов (Intercom, Drift и т.д.)
    try:
        await try_close_chat_widgets(page)
    except Exception:
        pass

    if scroll_only_mode_active:
        logger.info("🧭 Smart cursor: режим CTA_ANALYZER / SCROLL_ONLY (спуск с акцентом на CTA)")
        _hard_timeout_s = max(210, env_int("GSAP_SCROLL_FAILSAFE_MS", 180000) // 1000 + 30)
        try:
            reached_bottom, cursor_pos = await asyncio.wait_for(
                run_scroll_only_down_pass(
                    page=page,
                    viewport_width=viewport_width,
                    viewport_height=viewport_height,
                    total_time_ms=total_time_ms,
                    bottom_stable_rounds_required=bottom_stable_rounds_required,
                    scroll_speed_factor=scroll_speed_factor,
                    scroll_pause_min_ms=scroll_pause_min_ms,
                    scroll_pause_max_ms=scroll_pause_max_ms,
                    scroll_finish_timeout_ms=scroll_finish_timeout_ms,
                    require_bottom=True,
                    require_bottom_max_ms=smart_cursor_require_bottom_max_ms,
                    bottom_debug=bottom_debug,
                    stall_timeout_ms=strict_stall_timeout_ms,
                    cursor_pos=cursor_pos,
                ),
                timeout=float(_hard_timeout_s),
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"⚠️ [WARN] Достигнут лимит времени {_hard_timeout_s}s. "
                "Принудительное завершение скролла — FFmpeg сохранит видео."
            )
            reached_bottom = False
        if reached_bottom:
            logger.info("🧭 Smart cursor: режим CTA_ANALYZER завершён, страница прокручена до визуального конца")
        else:
            logger.warning("⚠️ Smart cursor: режим CTA_ANALYZER завершился по таймауту до достижения визуального конца страницы")
        return hovered_count

    strict_mode_active = strict_top_to_bottom_mode or always_descend
    if strict_mode_active:
        if always_descend and not strict_top_to_bottom_mode:
            logger.info("🧭 Smart cursor: включен ALWAYS_DESCEND, принудительно используем STRICT проход")
        logger.info("🧭 Smart cursor: STRICT режим (один проход сверху вниз, без переходов по страницам)")

        # ── Ожидание готовности контента: убеждаемся, что страница загрузила достаточно элементов ──
        # Также ждём исчезновения прелоадера/сплэш-экрана (если есть).
        # Агрессивная стратегия: быстрый опрос (500мс) + попытки dismiss прелоадера.
        content_wait_start = time.monotonic()
        content_wait_max_ms = int(os.getenv("PRELOAD_WAIT_MS", "10000"))
        content_wait_max_ms = max(4000, min(content_wait_max_ms, 60000))
        content_min_targets = 3
        _preloader_seen = False
        _dismiss_attempted = False
        _preloader_poll_count = 0
        while (time.monotonic() - content_wait_start) * 1000 < content_wait_max_ms:
            _preloader_poll_count += 1
            try:
                _probe_targets = await collect_interactive_targets(page, viewport_width, viewport_height, 50)
            except Exception:
                _probe_targets = []

            # Проверяем прелоадер: если он есть, ждём дольше
            _has_preloader = False
            if len(_probe_targets) < content_min_targets:
                try:
                    _has_preloader = await detect_preloader(page, viewport_width, viewport_height)
                except Exception:
                    pass
                if _has_preloader and not _preloader_seen:
                    logger.info("🧭 Smart cursor: обнаружен прелоадер, ждём загрузки контента...")
                    _preloader_seen = True

            if len(_probe_targets) >= content_min_targets and not _has_preloader:
                if _preloader_seen:
                    logger.info("✅ Smart cursor: прелоадер исчез, контент загружен")
                break

            # После 2с ожидания — пытаемся dismiss прелоадер кликом/клавишами
            elapsed_wait = int((time.monotonic() - content_wait_start) * 1000)
            if elapsed_wait >= 2000 and not _dismiss_attempted:
                _dismiss_attempted = True
                logger.info("🧭 Smart cursor: пытаемся dismiss прелоадер (click + keys)...")
                try:
                    # Кликаем по центру экрана
                    _cx = viewport_width * 0.5
                    _cy = viewport_height * 0.5
                    await page.mouse.click(_cx, _cy, delay=random.randint(20, 60))
                    await page.wait_for_timeout(200)
                except Exception:
                    pass
                # Пробуем клавиши для dismiss splash
                for _key in ["Escape", "Enter", "Space"]:
                    try:
                        await page.keyboard.press(_key)
                        await page.wait_for_timeout(100)
                    except Exception:
                        pass
                # Пробуем кликнуть entry-элементы
                try:
                    cursor_pos, _entry_ok, _entry_key = await try_click_entry_element(
                        page, cursor_pos, viewport_width, viewport_height, clicked_entry_keys,
                    )
                    if _entry_ok and _entry_key:
                        clicked_entry_keys.add(_entry_key)
                        logger.info("✅ Smart cursor: dismiss прелоадера через entry-клик")
                except Exception:
                    pass

            if elapsed_wait % 2000 < 600:
                logger.info(f"🧭 Smart cursor: ожидание загрузки контента ({len(_probe_targets)} элементов, {elapsed_wait}ms)...")
            await page.wait_for_timeout(500)

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
            nav_blacklist=nav_blacklist,
        )
        hovered_count += strict_hovered
        if reached_bottom:
            logger.info("🧭 Smart cursor: STRICT проход завершен, страница просмотрена до конца")
        else:
            logger.warning("⚠️ Smart cursor: STRICT проход завершился по hard-timeout до достижения конца страницы")

        if nav_tabs_visit_enabled and nav_tabs_max_visits > 0:
            if not reached_bottom:
                logger.warning("⚠️ Smart cursor: пропускаем вкладки навигации, пока главная страница не подтверждена до конца")
            else:
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
                            nav_blacklist=nav_blacklist,
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
                metrics = {
                    "scrollY": last_scroll_y,
                    "maxScroll": 0,
                    "atBottom": False,
                    "documentHeight": 0,
                    "viewportHeight": viewport_height,
                    "bottomGap": 0,
                }
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
            bottom_confirmed, bottom_reason = confirm_bottom_state_from_metrics(
                metrics=metrics,
                viewport_height=viewport_height,
                round_index=phase1_round,
                bottom_stable_rounds_required=bottom_stable_rounds_required,
                stagnant_rounds=stagnant_scroll_rounds,
            )
            if bottom_confirmed:
                bottom_stable_rounds += 1
            else:
                bottom_stable_rounds = 0

            if bottom_debug and (
                phase1_round % 8 == 0
                or (at_bottom and not bottom_confirmed)
                or bottom_confirmed
            ):
                logger.info(
                    "🧭 Phase-1 bottom check: "
                    f"atBottom={at_bottom}, confirmed={bottom_confirmed}, reason={bottom_reason}, "
                    f"scrollY={current_scroll_y}, maxScroll={int(metrics.get('maxScroll', 0))}, "
                    f"docHeight={metrics_document_height(metrics, viewport_height)}, stable={bottom_stable_rounds}"
                )

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
    if nav_tabs_visit_enabled and (main_page_scrolled or not scroll_to_end):
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
    elif nav_tabs_visit_enabled and scroll_to_end and not main_page_scrolled:
        logger.warning("⚠️ Smart cursor: пропускаем ФАЗУ 2, потому что главная страница не подтверждена до конца")

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
                    and not is_nav_blacklisted(target, nav_blacklist)
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
                        nav_blacklist=nav_blacklist,
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
                        if nav_blacklist is not None:
                            _bl_key = nav_blacklist_key(target)
                            nav_blacklist.add(_bl_key)
                            logger.info(f"🚫 Nav blacklist: добавлен '{_bl_key}' (phase-3 внешний)")
                        await ensure_page_within_allowed_site(
                            page,
                            site_url,
                            fallback_url=before_url,
                            timeout=15000,
                        )
                    elif navigated_internally and not allow_internal_nav_click:
                        logger.info("🖱️ Smart cursor: обнаружен внутренний переход, откатываемся назад")
                        if nav_blacklist is not None:
                            _bl_key = nav_blacklist_key(target)
                            nav_blacklist.add(_bl_key)
                            logger.info(f"🚫 Nav blacklist: добавлен '{_bl_key}' (phase-3 внутренний)")
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
                                nav_blacklist=nav_blacklist,
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


def _spawn_ffmpeg() -> Optional[subprocess.Popen]:
    """Запускает FFmpeg для захвата виртуального дисплея Xvfb.

    Строит команду из стандартных env-переменных (VIEWPORT_WIDTH, FFMPEG_FRAMERATE
    и т.д.) и возвращает subprocess.Popen-объект. Вызывается из main() после
    30-секундного прогрева страницы, до старта основного цикла прокрутки.
    """
    output_path = os.getenv('OUTPUT_PATH', '/app/output')
    os.makedirs(output_path, exist_ok=True)

    screen_width  = env_int('VIEWPORT_WIDTH',  1920)
    screen_height = env_int('VIEWPORT_HEIGHT', 1080)
    # libx264/yuv420p требует чётных размеров
    if screen_width  % 2 != 0:
        screen_width  -= 1
    if screen_height % 2 != 0:
        screen_height -= 1

    framerate_raw = env_int('FFMPEG_FRAMERATE', 30)
    framerate  = min(30, max(24, framerate_raw))
    preset     = 'superfast'
    crf        = env_int('FFMPEG_CRF', 24)
    threads_raw = env_int('FFMPEG_THREADS', 8)
    threads     = min(8, max(1, threads_raw))
    nice_level = max(-20, min(-5, env_int('FFMPEG_NICE_LEVEL', -5)))
    _dm_raw    = os.getenv('FFMPEG_DRAW_MOUSE', '0')
    draw_mouse = _dm_raw if _dm_raw in ('0', '1') else '0'
    display    = os.getenv('DISPLAY', ':99')

    crop_top  = env_int('FFMPEG_CROP_TOP', 0)
    auto_crop = env_bool('FFMPEG_AUTO_CROP_BROWSER_UI', True)
    if crop_top == 0 and auto_crop:
        crop_top = screen_height // 11
    if crop_top % 2 != 0:
        crop_top += 1

    timestamp  = datetime.now().strftime("%Y%m%d_%H%M%S")
    video_file = os.path.join(output_path, f"recording_{timestamp}.mp4")

    cmd: List[str] = []
    if os.name != 'nt' and shutil.which('nice'):
        cmd.extend(['nice', '-n', str(nice_level)])

    cmd.extend([
        'ffmpeg',
        '-f', 'x11grab',
        '-video_size', f'{screen_width}x{screen_height}',
        '-framerate', str(framerate),
        '-draw_mouse', draw_mouse,
        '-i', display,
    ])

    if crop_top > 0:
        crop_height = screen_height - crop_top
        if crop_height < 100:
            crop_height = 100
        if crop_height % 2 != 0:
            crop_height -= 1
        crop_top = screen_height - crop_height   # пересчёт после коррекции
        cmd.extend(['-vf', f'crop={screen_width}:{crop_height}:0:{crop_top}'])

    cmd.extend([
        '-c:v', 'libx264',
        '-preset', preset,
        '-crf', str(crf),
        '-threads', str(threads),
        '-pix_fmt', 'yuv420p',
        '-y',
        video_file,
    ])

    logger.info(f"🎥 FFmpeg command: {' '.join(cmd)}")
    logger.info(f"   Recording to: {video_file}")
    if crop_top > 0:
        logger.info(f"   Crop top: {crop_top}px")
    logger.info(f"   Framerate: {framerate}fps, preset={preset}, crf={crf}, nice={nice_level}, threads={threads}")

    try:
        ffmpeg_log = open('/tmp/ffmpeg.log', 'w', encoding='utf-8', errors='replace')
        proc = subprocess.Popen(cmd, stdout=ffmpeg_log, stderr=subprocess.STDOUT)
        logger.info(f"   FFmpeg PID: {proc.pid}")
        return proc
    except Exception as exc:
        logger.error(f"❌ Не удалось запустить FFmpeg: {exc}")
        return None


async def _stop_ffmpeg_process(ffmpeg_process: Optional[subprocess.Popen]) -> None:
    """Мягко завершает FFmpeg и затем принудительно закрывает процесс."""
    if ffmpeg_process is None:
        return

    logger.info("🎬 Остановка FFmpeg...")
    try:
        ffmpeg_process.send_signal(signal.SIGINT)
    except Exception as exc:
        logger.warning(f"⚠️ Не удалось отправить SIGINT в FFmpeg: {exc}")

    await asyncio.sleep(3)

    if ffmpeg_process.poll() is None:
        try:
            ffmpeg_process.kill()
        except Exception as exc:
            logger.warning(f"⚠️ Не удалось принудительно завершить FFmpeg: {exc}")

    try:
        ffmpeg_process.wait(timeout=5)
        logger.info("✅ FFmpeg остановлен")
    except Exception as exc:
        logger.warning(f"⚠️ Ошибка финализации FFmpeg: {exc}")


async def _run_preload_mode() -> None:
    """Режим предзагрузки: открываем сайт, ждём загрузки всего контента и выходим.

    Используется entrypoint.sh (PRELOAD_MODE=1) перед фактической записью,
    чтобы CDN/DNS-кэши прогрелись, анимации и медиа подгрузились.
    """
    target_url = os.getenv('TARGET_URL', 'https://www.gsproductions.co.za/')
    viewport_width = int(os.getenv('VIEWPORT_WIDTH', '1920'))
    viewport_height = int(os.getenv('VIEWPORT_HEIGHT', '1080'))
    load_timeout = int(os.getenv('LOAD_TIMEOUT', '60000'))
    preload_time_s = max(0, env_int('PRELOAD_TIME_S', 0))
    preload_wait_ms = max(5000, int(os.getenv('PRELOAD_WAIT_MS', '20000')))
    browser_performance_mode = env_bool('BROWSER_PERFORMANCE_MODE', True)

    logger.info(
        f"🔄 PRELOAD_MODE: загружаем {target_url} "
        f"(warm-up min={preload_wait_ms}ms, target={preload_time_s}s)..."
    )
    browser = None
    try:
        async with async_playwright() as p:
            browser_args = [
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage', '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-extensions', '--kiosk', '--start-fullscreen',
                f'--window-size={viewport_width},{viewport_height}',
                '--hide-crash-restore-bubble', '--disable-infobars',
                '--disable-frame-rate-limit',
                '--force-color-profile=srgb',
                '--disable-features=IsolateOrigins,site-per-process',
            ]
            if browser_performance_mode:
                browser_args.extend([
                    '--ignore-gpu-blocklist', '--enable-webgl',
                    '--enable-unsafe-swiftshader',
                    '--use-gl=angle',
                    '--use-angle=swiftshader',
                    '--disable-gpu-compositing',
                    '--enable-gpu-rasterization', '--enable-zero-copy',
                ])
            browser = await p.chromium.launch(headless=False, args=browser_args)
            context = await browser.new_context(
                viewport={'width': viewport_width, 'height': viewport_height},
                device_scale_factor=1,
                user_agent=_STEALTH_UA,
                locale='en-US',
                extra_http_headers={'Accept-Language': 'en-US,en;q=0.9'},
            )
            page = await context.new_page()
            if _STEALTH_AVAILABLE:
                try:
                    await _stealth_async(page)
                except Exception:
                    pass
            page_opened_at = time.monotonic()
            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=load_timeout)
                try:
                    await page.wait_for_load_state("networkidle", timeout=min(15000, load_timeout))
                except Exception:
                    pass
                page_opened_at = time.monotonic()
            except Exception as e:
                logger.warning(f"⚠️ PRELOAD_MODE goto: {e}")
                page_opened_at = time.monotonic()
            # Прокручиваем страницу вниз/вверх, чтобы триггернуть lazyload медиа
            try:
                for _ in range(4):
                    await page.evaluate("() => window.scrollBy(0, Math.round(window.innerHeight * 0.8))")
                    await page.wait_for_timeout(600)
                await page.evaluate("() => window.scrollTo({ top: 0, behavior: 'auto' })")
            except Exception:
                pass
            target_hold_ms = max(preload_wait_ms, preload_time_s * 1000)
            elapsed_after_open_ms = int((time.monotonic() - page_opened_at) * 1000)
            remaining_wait_ms = max(0, target_hold_ms - elapsed_after_open_ms)
            if remaining_wait_ms > 0:
                await page.wait_for_timeout(remaining_wait_ms)
            logger.info(f"✅ PRELOAD_MODE: прогрев завершён ({target_hold_ms}ms после открытия сайта)")
            await context.close()
    except Exception as e:
        logger.warning(f"⚠️ PRELOAD_MODE error: {e}")
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass


async def main():
    """Главная функция для рендеринга веб-сайта на сервере с Xvfb и FFmpeg видеозаписью."""
    browser = None
    ffmpeg_proc: Optional[subprocess.Popen] = None

    # ── PRELOAD_MODE: прогрев без FFmpeg-записи (используется entrypoint.sh) ──
    preload_mode = env_bool('PRELOAD_MODE', False)
    if preload_mode:
        await _run_preload_mode()
        return

    try:
        # Получение конфигурации из переменных окружения
        output_path = os.getenv('OUTPUT_PATH', 'output')
        target_url = os.getenv('TARGET_URL', 'https://www.gsproductions.co.za/')
        viewport_width = int(os.getenv('VIEWPORT_WIDTH', '1920'))
        viewport_height = int(os.getenv('VIEWPORT_HEIGHT', '1080'))
        render_timeout = int(os.getenv('RENDER_TIMEOUT', '2000'))
        load_timeout = int(os.getenv('LOAD_TIMEOUT', '60000'))
        smart_cursor_enabled = env_bool('SMART_CURSOR_ENABLED', True)
        smart_cursor_timeout = int(os.getenv('SMART_CURSOR_TIMEOUT', '360000'))
        smart_cursor_max_targets = int(os.getenv('SMART_CURSOR_MAX_TARGETS', '0'))
        hover_min_ms = int(os.getenv('SMART_CURSOR_HOVER_MIN_MS', '220'))
        hover_max_ms = int(os.getenv('SMART_CURSOR_HOVER_MAX_MS', '760'))
        smart_cursor_mode = resolve_smart_cursor_mode(os.getenv('SMART_CURSOR_MODE', 'default'))
        entry_click_enabled = env_bool('SMART_CURSOR_ENTRY_CLICK_ENABLED', True)
        entry_click_attempts = int(os.getenv('SMART_CURSOR_ENTRY_CLICK_ATTEMPTS', '3'))
        scroll_to_end = env_bool('SMART_CURSOR_SCROLL_TO_END', True)
        bottom_stable_rounds_required = int(os.getenv('SMART_CURSOR_BOTTOM_STABLE_ROUNDS', '4'))
        scroll_speed_factor = float(os.getenv('SMART_CURSOR_SCROLL_SPEED', '1.25'))
        scroll_pause_min_ms = int(os.getenv('SMART_CURSOR_SCROLL_PAUSE_MIN_MS', '35'))
        scroll_pause_max_ms = int(os.getenv('SMART_CURSOR_SCROLL_PAUSE_MAX_MS', '55'))
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
        logger.info(f"🧭 Smart cursor mode: {smart_cursor_mode}")
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
                '--disable-setuid-sandbox',
                '--disable-extensions',
                '--kiosk',
                '--start-fullscreen',
                '--start-maximized',
                '--window-position=0,0',
                f'--window-size={viewport_width},{viewport_height}',
                '--hide-crash-restore-bubble',
                '--disable-infobars',
                '--disable-frame-rate-limit',
                '--force-color-profile=srgb',
                '--disable-features=IsolateOrigins,site-per-process',
            ]

            if browser_performance_mode:
                browser_args.extend([
                    '--ignore-gpu-blocklist',
                    '--enable-webgl',
                    '--enable-unsafe-swiftshader',
                    '--use-gl=angle',           # ANGLE программный WebGL-рендерер
                    '--use-angle=swiftshader',  # заставляет SwiftShader использовать многопоточность CPU
                    '--enable-gpu-rasterization',
                    '--enable-zero-copy',
                    '--disable-gpu-compositing',
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
                device_scale_factor=1,
                user_agent=_STEALTH_UA,
                locale='en-US',
                extra_http_headers={'Accept-Language': 'en-US,en;q=0.9'},
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
                                el.style.willChange = 'transform';
                                el.style.backfaceVisibility = 'hidden';
                                el.style.contain = 'layout style paint';
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

                        const cursorState = {
                            x: -100,
                            y: -100,
                            targetX: -100,
                            targetY: -100,
                            rafId: 0,
                        };

                        function paintCursor(force) {
                            cursorState.rafId = 0;
                            const el = ensureCursor();
                            const mx = Number.isFinite(cursorState.targetX) ? Math.round(cursorState.targetX) : cursorState.x;
                            const my = Number.isFinite(cursorState.targetY) ? Math.round(cursorState.targetY) : cursorState.y;
                            if (!force && mx === cursorState.x && my === cursorState.y) {
                                return;
                            }
                            cursorState.x = mx;
                            cursorState.y = my;
                            el.style.transform = `translate3d(${mx}px, ${my}px, 0)`;
                        }

                        function scheduleCursorPaint() {
                            if (cursorState.rafId) {
                                return;
                            }
                            cursorState.rafId = window.requestAnimationFrame(() => paintCursor(false));
                        }

                        function moveCursor(x, y, immediate = false) {
                            cursorState.targetX = Number.isFinite(x) ? x : cursorState.targetX;
                            cursorState.targetY = Number.isFinite(y) ? y : cursorState.targetY;
                            if (immediate) {
                                if (cursorState.rafId) {
                                    window.cancelAnimationFrame(cursorState.rafId);
                                    cursorState.rafId = 0;
                                }
                                paintCursor(true);
                                return;
                            }
                            scheduleCursorPaint();
                        }

                        window.__vpvoaeEnsureCursor = ensureCursor;
                        window.__vpvoaeMoveCursor = moveCursor;

                        const pointerHandler = (ev) => {
                            const ex = Number(ev && ev.clientX);
                            const ey = Number(ev && ev.clientY);
                            moveCursor(Number.isFinite(ex) ? ex : 0, Number.isFinite(ey) ? ey : 0, false);
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

            if _STEALTH_AVAILABLE:
                try:
                    await _stealth_async(page)
                    logger.info("🕵️ playwright-stealth применён, fingerprint замаскирован")
                except Exception as _se:
                    logger.warning(f"⚠️ playwright-stealth не удалось применить: {_se}")

            if visible_cursor_enabled:
                try:
                    await page.evaluate("""() => { if (window.__vpvoaeEnsureCursor) window.__vpvoaeEnsureCursor(); }""")
                except Exception:
                    pass

            logger.info(f"📄 Открываем целевой сайт: {target_url}")
            # Загружаем до DOM-ready. Глубокий networkidle-check выполняется позже,
            # непосредственно перед стартом FFmpeg.

            # —— Прогрев мыши перед навигацией (обман поведенческого анализа) ——
            try:
                for _wx, _wy in [
                    (random.randint(200, 600), random.randint(200, 500)),
                    (random.randint(700, 1200), random.randint(300, 700)),
                    (random.randint(400, 900), random.randint(400, 600)),
                ]:
                    await page.mouse.move(_wx, _wy, steps=random.randint(6, 12))
                    await asyncio.sleep(random.uniform(0.05, 0.13))
            except Exception:
                pass

            # —— goto с повторными попытками (3 попытки, пауза 5с) ——
            _goto_ok = False
            for _attempt in range(1, 4):
                try:
                    await page.goto(
                        target_url,
                        wait_until="domcontentloaded",
                        timeout=load_timeout,
                    )
                    _goto_ok = True
                    break
                except Exception as _ge:
                    logger.warning(f"⚠️ goto попытка {_attempt}/3: {_ge}")
                    if _attempt < 3:
                        await asyncio.sleep(5)
            if not _goto_ok:
                logger.warning("⚠️ Все 3 попытки goto не удались — продолжаем без загрузки...")
            else:
                if visible_cursor_enabled:
                    try:
                        await page.evaluate("""() => { if (window.__vpvoaeEnsureCursor) window.__vpvoaeEnsureCursor(); }""")
                    except Exception:
                        pass
                logger.info("✅ Сайт загружен успешно")

            # ── Курсор появляется сразу после загрузки страницы ──
            # Позиционируем его заранее, чтобы он был виден с первых секунд записи.
            _entry_cx = viewport_width * random.uniform(0.36, 0.54)
            _entry_cy = viewport_height * random.uniform(0.22, 0.38)
            if visible_cursor_enabled:
                try:
                    await page.evaluate(
                        """([mx, my]) => { if (window.__vpvoaeMoveCursor) window.__vpvoaeMoveCursor(mx, my); }""",
                        [_entry_cx, _entry_cy],
                    )
                    await page.mouse.move(_entry_cx, _entry_cy)
                except Exception:
                    pass

            if browser_fullscreen:
                try:
                    await page.bring_to_front()
                    await page.wait_for_timeout(150)
                    await page.keyboard.press("F11")
                    await page.wait_for_timeout(450)
                except Exception:
                    logger.warning("⚠️ Не удалось переключить браузер в fullscreen")

            await wait_for_deep_page_ready(
                page,
                load_timeout_ms=load_timeout,
                post_networkidle_pause_ms=10000,
            )
            await perform_prerender_scroll(page)

            try:
                await page.mouse.move(500, 500)
            except Exception:
                pass
            logger.info("⏳ Прогрев страницы: 30 секунд после пробуждения скриптов...")
            await page.wait_for_timeout(30000)

            if visible_cursor_enabled:
                try:
                    await page.evaluate(
                        """([mx, my]) => { if (window.__vpvoaeMoveCursor) window.__vpvoaeMoveCursor(mx, my); }""",
                        [_entry_cx, _entry_cy],
                    )
                    await page.mouse.move(_entry_cx, _entry_cy)
                except Exception:
                    pass

            # ── Запуск FFmpeg прямо из Python ────────────────────────────────────
            # Deep Hydration: ждём появления контента перед записью
            try:
                for _hydration_check in range(8):  # макс ~4с
                    _hydrated = await page.evaluate("""() => {
                        const body = document.body;
                        if (!body) return false;
                        const style = window.getComputedStyle(body);
                        if (parseFloat(style.opacity || '1') < 0.1) return false;
                        if (document.querySelector('h1') || document.querySelector('img')) return true;
                        if (body.innerText && body.innerText.trim().length > 50) return true;
                        return false;
                    }""")
                    if _hydrated:
                        logger.info(f"✅ Deep Hydration: контент обнаружен за {(_hydration_check + 1) * 0.5:.1f}с")
                        break
                    await asyncio.sleep(0.5)
                else:
                    logger.warning("⚠️ Deep Hydration: контент не обнаружен, продолжаем всё равно")
            except Exception as _he:
                logger.warning(f"⚠️ Deep Hydration ошибка: {_he}")
            await asyncio.sleep(1)

            logger.info("🎥 Запуск FFmpeg записи...")
            ffmpeg_proc = _spawn_ffmpeg()
            if ffmpeg_proc is None:
                raise RuntimeError("FFmpeg не удалось запустить — прерываем рендер")
            logger.info("⏳ Даём FFmpeg 2 секунды на инициализацию файла...")
            await asyncio.sleep(2)
            if ffmpeg_proc.poll() is not None:
                raise RuntimeError(
                    "FFmpeg завершился преждевременно после старта — смотрите /tmp/ffmpeg.log"
                )
            logger.info("✅ FFmpeg успешно записывает")

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
                    smart_cursor_mode=smart_cursor_mode,
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

            if ffmpeg_proc is not None:
                await _stop_ffmpeg_process(ffmpeg_proc)
                ffmpeg_proc = None

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
            sys.exit(0)

    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
        sys.exit(1)
    finally:
        if ffmpeg_proc is not None:
            await _stop_ffmpeg_process(ffmpeg_proc)
            ffmpeg_proc = None
        if browser:
            try:
                await browser.close()
                logger.info("🛑 Браузер закрыт")
            except Exception as e:
                logger.warning(f"Ошибка при закрытии браузера: {e}")


if __name__ == "__main__":
    logger.info("=" * 60)
    asyncio.run(main())
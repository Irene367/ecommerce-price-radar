#!/usr/bin/env python3
"""Fetch visible text from user-provided public URLs for ecommerce analysis.

Safety boundaries:
- Only accepts explicit user-provided http:// or https:// URLs.
- Processes at most 10 URLs per run.
- Uses Python standard library only; no requests, bs4, Selenium, or JS runtime.
- Does not log in, does not use cookies, and does not read browser data.
- Does not bypass CAPTCHA, anti-bot systems, login walls, paywalls, or platform limits.
- Does not execute JavaScript and does not use headless browsers.
- Extracted price, stock, discount, shipping, and review candidates are hints only;
  business-critical fields must be reviewed by the user.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


MAX_URLS = 10
TIMEOUT_SECONDS = 10
USER_AGENT = "EcommercePriceRadarSkill/1.3 (+public-page-analysis; no-login; no-bypass)"
MAX_TEXT_CHARS = 5000
MAX_DOWNLOAD_BYTES = 2_000_000

LOGIN_PATTERNS = re.compile(
    r"(登录|登陆|sign\s*in|log\s*in|login|账号|密码|注册|请先登录|会员登录)",
    re.IGNORECASE,
)
CAPTCHA_PATTERNS = re.compile(
    r"(验证码|captcha|人机验证|安全验证|滑块|verify you are human|robot check)",
    re.IGNORECASE,
)
JS_RENDER_PATTERNS = re.compile(
    r"(enable javascript|请启用javascript|app-root|__next|nuxt|window\.__|需要.*javascript)",
    re.IGNORECASE,
)

PRICE_PATTERN = re.compile(
    r"(?:¥|￥|RMB|CNY)?\s*(?:[1-9]\d{0,5})(?:\.\d{1,2})?\s*(?:元|块)?"
)
DISCOUNT_PATTERN = re.compile(r".{0,12}(?:券|优惠|满减|立减|折扣|补贴|秒杀|活动|促销).{0,30}")
SHIPPING_PATTERN = re.compile(r".{0,12}(?:发货|现货|包邮|运费|次日达|小时内|顺丰|物流).{0,30}")
STOCK_PATTERN = re.compile(r".{0,12}(?:库存|有货|缺货|售罄|补货|仅剩|现货).{0,30}")
REVIEW_PATTERN = re.compile(r".{0,12}(?:评价|评分|评论|好评|差评|星|review|rating).{0,30}", re.IGNORECASE)


class VisibleTextParser(HTMLParser):
    """Small HTML parser that extracts title, description meta, and visible text."""

    SKIP_TAGS = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.in_title = False
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.meta_description = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return
        if tag == "title":
            self.in_title = True
            return
        if tag == "meta":
            attr_map = {name.lower(): (value or "") for name, value in attrs}
            name = attr_map.get("name", "").lower()
            prop = attr_map.get("property", "").lower()
            if name == "description" or prop == "og:description":
                content = attr_map.get("content", "")
                if content and not self.meta_description:
                    self.meta_description = normalize_space(content)
        if tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "section", "article"}:
            self.text_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
            return
        if tag == "title":
            self.in_title = False
        if tag in {"p", "div", "li", "tr", "h1", "h2", "h3", "section", "article"}:
            self.text_parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.in_title:
            self.title_parts.append(data)
        self.text_parts.append(data)

    def parsed(self) -> tuple[str, str, str]:
        title = normalize_space(" ".join(self.title_parts))
        text = normalize_space(" ".join(self.text_parts))
        return title, self.meta_description, text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch visible text from up to 10 user-provided public URLs."
    )
    parser.add_argument("--url", action="append", help="A single public URL. Can be repeated.")
    parser.add_argument("--urls-file", help="Text file containing one URL per line.")
    parser.add_argument("--output", help="Output JSON file path. Defaults to stdout.")
    return parser.parse_args()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_error(message: str) -> dict[str, Any]:
    return {
        "generated_at": now_iso(),
        "max_urls": MAX_URLS,
        "processed_count": 0,
        "warnings": [],
        "results": [],
        "error": message,
    }


def load_urls(args: argparse.Namespace) -> list[str]:
    urls: list[str] = []
    if args.url:
        urls.extend(args.url)
    if args.urls_file:
        try:
            with open(args.urls_file, "r", encoding="utf-8-sig") as handle:
                urls.extend(line.strip() for line in handle if line.strip())
        except OSError as exc:
            raise ValueError(f"无法读取 urls-file：{exc}") from exc

    cleaned: list[str] = []
    for url in urls:
        value = url.strip().lstrip("\ufeff")
        if value and not value.startswith("#"):
            cleaned.append(value)
    return cleaned


def validate_url(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return "URL 仅允许 http:// 或 https://"
    if not parsed.netloc:
        return "URL 缺少域名"
    return None


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text)).strip()


def trim_matches(matches: list[str], limit: int = 12) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for match in matches:
        item = normalize_space(match)
        if not item or item in seen:
            continue
        seen.add(item)
        output.append(item[:120])
        if len(output) >= limit:
            break
    return output


def extract_candidates(text: str) -> dict[str, list[str]]:
    return {
        "prices": trim_matches(PRICE_PATTERN.findall(text), 20),
        "discount_keywords": trim_matches(DISCOUNT_PATTERN.findall(text), 12),
        "shipping_keywords": trim_matches(SHIPPING_PATTERN.findall(text), 12),
        "stock_keywords": trim_matches(STOCK_PATTERN.findall(text), 12),
        "review_keywords": trim_matches(REVIEW_PATTERN.findall(text), 12),
    }


def detect_warnings(status_code: int | None, text: str, html: str) -> list[str]:
    warnings: list[str] = []
    haystack = f"{text[:3000]} {html[:3000]}"
    if status_code in {401, 403}:
        warnings.append("页面可能需要登录、权限或被平台限制，已停止深入读取。")
    elif status_code == 404:
        warnings.append("页面返回 404，可能链接失效。")
    elif status_code and status_code >= 500:
        warnings.append("页面服务器返回 5xx 错误。")
    if LOGIN_PATTERNS.search(haystack):
        warnings.append("疑似登录页面或登录墙；不尝试登录。")
    if CAPTCHA_PATTERNS.search(haystack):
        warnings.append("疑似验证码或人机验证；不尝试绕过。")
    if len(text) < 200:
        warnings.append("页面可见文本过短，可能是强 JS 渲染、登录墙或平台限制。")
    if JS_RENDER_PATTERNS.search(haystack) and len(text) < 1000:
        warnings.append("疑似强 JavaScript 渲染；脚本不执行页面 JavaScript。")
    return warnings


def decode_body(body: bytes, content_type: str) -> str:
    charset = ""
    match = re.search(r"charset=([\w.\-]+)", content_type, re.IGNORECASE)
    if match:
        charset = match.group(1)
    for encoding in [charset, "utf-8", "gb18030", "latin-1"]:
        if not encoding:
            continue
        try:
            return body.decode(encoding, errors="replace")
        except LookupError:
            continue
    return body.decode("utf-8", errors="replace")


def parse_html(html: str) -> tuple[str, str, str]:
    parser = VisibleTextParser()
    parser.feed(html)
    parser.close()
    return parser.parsed()


def empty_result(input_url: str, warning: str = "", error: str = "") -> dict[str, Any]:
    warnings = [warning] if warning else []
    return {
        "input_url": input_url,
        "final_url": "",
        "status_code": None,
        "title": "",
        "meta_description": "",
        "visible_text_snippet": "",
        "extracted_candidates": {
            "prices": [],
            "discount_keywords": [],
            "shipping_keywords": [],
            "stock_keywords": [],
            "review_keywords": [],
        },
        "warnings": warnings,
        "error": error,
    }


def build_result(
    input_url: str,
    final_url: str,
    status_code: int | None,
    title: str,
    meta_description: str,
    visible_text: str,
    html: str,
    extra_warnings: list[str] | None = None,
    error: str = "",
) -> dict[str, Any]:
    warnings = detect_warnings(status_code, visible_text, html)
    if extra_warnings:
        warnings.extend(extra_warnings)
    if status_code and status_code >= 400:
        warnings.append("HTTP 状态码异常；不把该页面内容作为可靠事实。")
        if not error:
            error = f"HTTP {status_code}"
    return {
        "input_url": input_url,
        "final_url": final_url,
        "status_code": status_code,
        "title": title,
        "meta_description": meta_description,
        "visible_text_snippet": visible_text[:MAX_TEXT_CHARS],
        "extracted_candidates": extract_candidates(visible_text),
        "warnings": warnings,
        "error": error,
    }


def fetch_one(url: str) -> dict[str, Any]:
    validation_error = validate_url(url)
    if validation_error:
        return empty_result(url, validation_error, validation_error)

    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.5",
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status_code = getattr(response, "status", None) or response.getcode()
            final_url = response.geturl()
            content_type = response.headers.get("content-type", "")
            body = response.read(MAX_DOWNLOAD_BYTES + 1)
            extra_warnings: list[str] = []
            if len(body) > MAX_DOWNLOAD_BYTES:
                body = body[:MAX_DOWNLOAD_BYTES]
                extra_warnings.append("页面内容较大，已截断读取前 2MB。")
            html = decode_body(body, content_type)
            title, meta_description, visible_text = parse_html(html)
            return build_result(
                input_url=url,
                final_url=final_url,
                status_code=status_code,
                title=title,
                meta_description=meta_description,
                visible_text=visible_text,
                html=html,
                extra_warnings=extra_warnings,
            )
    except HTTPError as exc:
        body = b""
        try:
            body = exc.read(MAX_DOWNLOAD_BYTES)
        except Exception:
            body = b""
        content_type = exc.headers.get("content-type", "") if exc.headers else ""
        html = decode_body(body, content_type) if body else ""
        title, meta_description, visible_text = parse_html(html) if html else ("", "", "")
        return build_result(
            input_url=url,
            final_url=exc.geturl() or url,
            status_code=exc.code,
            title=title,
            meta_description=meta_description,
            visible_text=visible_text,
            html=html,
            error=f"HTTP {exc.code}",
        )
    except (URLError, TimeoutError, socket.timeout) as exc:
        return empty_result(
            url,
            "读取失败，请用户粘贴页面内容、截图文字或整理表格。",
            str(exc),
        )
    except Exception as exc:  # pragma: no cover - defensive stability
        return empty_result(
            url,
            "页面读取或解析失败，请用户粘贴页面内容、截图文字或整理表格。",
            str(exc),
        )


def main() -> int:
    args = parse_args()
    try:
        urls = load_urls(args)
    except ValueError as exc:
        payload = stable_error(str(exc))
    else:
        warnings: list[str] = []
        if not urls:
            payload = stable_error("请通过 --url 或 --urls-file 提供至少 1 个 URL。")
        else:
            if len(urls) > MAX_URLS:
                warnings.append("本次仅处理前 10 个 URL，剩余 URL 请分批处理。")
            selected = urls[:MAX_URLS]
            payload = {
                "generated_at": now_iso(),
                "max_urls": MAX_URLS,
                "processed_count": len(selected),
                "warnings": warnings,
                "results": [fetch_one(url) for url in selected],
            }

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.write("\n")
        except OSError as exc:
            print(json.dumps(stable_error(f"无法写入 output：{exc}"), ensure_ascii=False), file=sys.stderr)
            return 1
    else:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

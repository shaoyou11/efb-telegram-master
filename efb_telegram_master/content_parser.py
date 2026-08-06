"""Convert common WeChat card HTML into safe Telegram HTML."""

import html
from html.parser import HTMLParser
from urllib.parse import urlsplit


_ALLOWED_SCHEMES = {"http", "https", "tg", "mailto"}


def _safe_href(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    scheme = urlsplit(value).scheme.lower()
    return value if scheme in _ALLOWED_SCHEMES else ""


class _TelegramHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.anchor_depth = 0
        self.anchor_href = ""

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attributes = dict(attrs)
        if tag == "a":
            self.anchor_href = _safe_href(attributes.get("href", ""))
            self.anchor_depth = 1
            if self.anchor_href:
                self.parts.append(
                    '<a href="%s">' % html.escape(self.anchor_href, quote=True)
                )
        elif tag in {"br", "hr"}:
            self.parts.append("\n")
        elif tag in {"p", "div", "li"} and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "a" and self.anchor_depth:
            if self.anchor_href:
                self.parts.append("</a>")
            self.anchor_depth = 0
            self.anchor_href = ""
        elif tag in {"p", "div", "li"} and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(html.escape(data, quote=False))


def normalize_wechat_html(text: str) -> str:
    """Keep readable text and safe links; unsupported wx links become plain text."""
    if not text:
        return text
    parser = _TelegramHTMLParser()
    try:
        parser.feed(str(text))
        parser.close()
    except (ValueError, TypeError):
        return html.escape(str(text), quote=False)
    return "".join(parser.parts).strip()

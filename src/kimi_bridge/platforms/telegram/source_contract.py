"""Credential-free projection of the official Telegram Bot API reference."""

from __future__ import annotations

import re
from html.parser import HTMLParser

from ..source_contract import (
    PlatformInspection,
    SourceCheck,
    SourceEvidence,
    SourceFetcher,
    SourceRequest,
    fetch_for_check,
)


_REFERENCE = SourceRequest(
    "https://core.telegram.org/bots/api", "live", 5 * 1024 * 1024
)
_METHODS = {
    "getme": (),
    "getwebhookinfo": ("webhookinfo",),
    "getupdates": ("offset", "timeout", "allowed_updates"),
    "sendmessage": ("chat_id", "text"),
    "senddocument": ("chat_id", "document"),
}


async def inspect(fetcher: SourceFetcher) -> PlatformInspection:
    sources: list[SourceEvidence] = []
    checks: list[SourceCheck] = []
    source = await fetch_for_check(
        fetcher, _REFERENCE, "telegram.reference", sources, checks
    )
    if source is None:
        return PlatformInspection("telegram", tuple(sources), tuple(checks))
    try:
        parser = _BotApiParser()
        parser.feed(source.text())
        parser.close()
    except (UnicodeDecodeError, ValueError):
        checks.append(
            SourceCheck(
                "telegram.reference",
                "drift",
                "official Bot API reference is not parseable UTF-8 HTML",
                (source.url,),
            )
        )
        return PlatformInspection("telegram", tuple(sources), tuple(checks))

    document = " ".join(parser.document).casefold()
    envelope_fields = ("ok", "result", "description", "error_code")
    missing = tuple(
        field
        for field in envelope_fields
        if re.search(rf"(?<![a-z0-9_]){re.escape(field)}(?![a-z0-9_])", document)
        is None
    )
    checks.append(
        SourceCheck(
            "telegram.response-envelope",
            "drift" if missing else "matched",
            (
                "official Bot API reference omitted response envelope fields: "
                + ", ".join(missing)
                if missing
                else "official reference retains the Bot API response envelope"
            ),
            (source.url,),
        )
    )
    for method, required in _METHODS.items():
        section = " ".join(parser.sections.get(method, ())).casefold()
        missing_fields = tuple(field for field in required if field not in section)
        if not section:
            detail = f"official Bot API reference omitted the {method} section"
            state = "drift"
        elif missing_fields:
            detail = (
                f"official {method} section omitted fields: "
                + ", ".join(missing_fields)
            )
            state = "drift"
        else:
            detail = f"official reference retains the {method} method surface"
            state = "matched"
        checks.append(
            SourceCheck(
                f"telegram.method.{method}", state, detail, (source.url,)
            )
        )
    return PlatformInspection("telegram", tuple(sources), tuple(checks))


class _BotApiParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.document: list[str] = []
        self.sections: dict[str, list[str]] = {}
        self._heading_tag: str | None = None
        self._heading: list[str] = []
        self._section: str | None = None
        self._ignored_depth = 0

    def handle_starttag(
        self, tag: str, _attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        elif self._ignored_depth == 0 and tag in {"h3", "h4"}:
            self._heading_tag = tag
            self._heading = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag == self._heading_tag:
            heading = " ".join(self._heading).strip().casefold()
            self._section = heading
            self.sections.setdefault(heading, [])
            self._heading_tag = None
            self._heading = []

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        normalized = " ".join(data.split())
        if not normalized:
            return
        self.document.append(normalized)
        if self._heading_tag is not None:
            self._heading.append(normalized)
        elif self._section is not None:
            self.sections[self._section].append(normalized)

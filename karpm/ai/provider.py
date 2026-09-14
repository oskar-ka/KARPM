"""The seam between a pass and whoever answers it.

A pass says what it wants in neutral terms - some text, some images, a schema to
fill in - and a provider turns that into whatever its API expects. Nothing above
this line knows what an Anthropic content block looks like, which is what makes
it possible to put a different model behind a pass later without touching the
pass itself.

Two of the three passes are text-only, so a provider with no vision at all can
still serve them. `supports_images` is how a pass finds that out before sending
something that would fail.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """The provider could not answer. Retryable or not, the caller decides."""


@dataclass
class Reply:
    """What came back, and what it cost."""

    data: dict
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def text(value: str) -> dict:
    return {"type": "text", "text": value}


def image(path: str | Path) -> dict:
    """An image by path. The provider reads and encodes it however it must."""
    return {"type": "image", "path": str(path)}


@dataclass
class Request:
    """One question, in terms no particular API owns."""

    system: str
    blocks: list[dict]
    schema: Any                     # a pydantic model class
    model: str
    effort: str = "medium"
    max_tokens: int = 4000
    # The system prompt is identical for every listing in a run, so it is worth
    # caching where the provider can.
    cache_system: bool = True
    extra: dict = field(default_factory=dict)

    @property
    def has_images(self) -> bool:
        return any(block["type"] == "image" for block in self.blocks)


class Provider(Protocol):
    name: str

    def supports_images(self) -> bool:
        ...

    def complete(self, request: Request) -> Reply:
        ...


# What a suffix on disk means when the bytes are sent. karpm.images reads this
# same table rather than keeping one of its own.
MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".gif": "image/gif"}


# --- Anthropic ------------------------------------------------------------

class AnthropicProvider:
    """Claude, through the official SDK."""

    name = "anthropic"

    def __init__(self, client=None) -> None:
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    def supports_images(self) -> bool:
        return True

    def complete(self, request: Request) -> Reply:
        import anthropic

        system = [{"type": "text", "text": request.system}]
        if request.cache_system:
            system[0]["cache_control"] = {"type": "ephemeral"}

        try:
            response = self.client.messages.parse(
                model=request.model,
                max_tokens=request.max_tokens,
                system=system,
                thinking={"type": "adaptive"},
                output_config={"effort": request.effort},
                messages=[{"role": "user", "content": self._content(request.blocks)}],
                output_format=request.schema,
            )
        except anthropic.APIError as exc:
            raise ProviderError(f"{request.model}: {exc}") from exc

        if response.stop_reason == "refusal":
            detail = getattr(response, "stop_details", None)
            raise ProviderError(
                f"{request.model} declined: {getattr(detail, 'category', 'unknown')}")

        usage = response.usage
        return Reply(
            data=response.parsed_output.model_dump(),
            model=response.model,
            input_tokens=getattr(usage, "input_tokens", 0),
            output_tokens=getattr(usage, "output_tokens", 0),
            cached_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )

    def _content(self, blocks: list[dict]) -> list[dict]:
        out = []
        for block in blocks:
            if block["type"] == "text":
                out.append({"type": "text", "text": block["text"]})
                continue
            path = Path(block["path"])
            out.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": MEDIA_TYPES.get(path.suffix.lower(), "image/jpeg"),
                    "data": base64.standard_b64encode(path.read_bytes()).decode("ascii"),
                },
            })
        return out


PROVIDERS = {"anthropic": AnthropicProvider}


def get(name: str) -> Provider:
    """A provider by name, as written in the config."""
    try:
        return PROVIDERS[name]()
    except KeyError:
        raise ProviderError(
            f"unknown provider {name!r}; known: {', '.join(sorted(PROVIDERS))}") from None

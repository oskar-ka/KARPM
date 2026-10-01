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

from . import models

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


def image_stub(label: str) -> dict:
    """An image that is not there: `label` goes where the bytes would.

    Only for showing what a request looks like. A pass never builds one, and a
    provider that is really sending would be sending a label to the API.
    """
    return {"type": "image", "stub": label}


@dataclass
class Request:
    """One question, in terms no particular API owns."""

    system: str
    blocks: list[dict]
    schema: Any                     # a pydantic model class
    model: str
    effort: str = "medium"
    # None means not sent, which is what the thinking models require: they
    # reject the parameter outright. The provider leaves it out for those
    # whatever this says, so a value set for one model survives a look at
    # another.
    temperature: float | None = None
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

    def wire_body(self, request: Request) -> str:
        """The request as it would go over the wire, byte for byte, unsent."""
        ...


# What a suffix on disk means when the bytes are sent. karpm.images reads this
# same table rather than keeping one of its own.
MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".gif": "image/gif"}


# --- Anthropic ------------------------------------------------------------

def _thinking_for(request: Request) -> dict:
    """The thinking and effort parameters this model accepts - or neither.

    Not every model takes `thinking: adaptive` and an effort level, and one that
    does not rejects the whole request rather than ignoring what it cannot use.
    The catalogue decides. A model missing from it is assumed to take both,
    since that is what a model newer than the table will be.
    """
    known = models.get(request.model)
    if known is not None and not known.thinking:
        return {}
    effort = request.effort
    if known is not None and effort not in known.effort:
        effort = "high"                 # what the API itself defaults to
    return {"thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}


def _sampling_for(request: Request) -> dict:
    """The temperature, for a model that takes one - or nothing.

    The adaptive-thinking generation rejects any sampling parameter with a
    400, so it is sent only to a model the catalogue says still has one. A
    model missing from the catalogue is assumed to be of the newer kind.
    """
    known = models.get(request.model)
    if request.temperature is None or known is None or not known.sampling:
        return {}
    # The SDK no longer has a keyword for it, the parameter being gone from
    # every current model; `extra_body` puts it in the request all the same.
    return {"extra_body": {"temperature": request.temperature}}


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

        try:
            response = self._send(self.client, request)
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

    def wire_body(self, request: Request) -> str:
        """The JSON the SDK would put on the wire for this request.

        Produced by the SDK itself, against a transport that records the
        request and answers it with an empty message, rather than assembled
        here from what the SDK is believed to do. The one is what the API
        gets; the other is a guess that drifts.
        """
        import anthropic
        import httpx2 as httpx

        captured: dict = {}

        def record(http_request):
            captured["body"] = http_request.content.decode("utf-8")
            return httpx.Response(200, json={
                "id": "msg_unsent", "type": "message", "role": "assistant",
                "model": request.model, "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "{}"}],
                "usage": {"input_tokens": 0, "output_tokens": 0},
            })

        client = anthropic.Anthropic(
            api_key="unsent",
            http_client=anthropic.DefaultHttpxClient(transport=httpx.MockTransport(record)),
            max_retries=0,
        )
        try:
            self._send(client, request)
        except Exception:
            # The empty answer fails the schema; by then the request has been
            # recorded, which is all that was wanted of the call.
            if "body" not in captured:
                raise
        return captured["body"]

    def _send(self, client, request: Request):
        """One call, built the same way whoever is on the other end."""
        return client.messages.parse(**self._params(request))

    def _params(self, request: Request) -> dict:
        system = [{"type": "text", "text": request.system}]
        if request.cache_system:
            system[0]["cache_control"] = {"type": "ephemeral"}
        return {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": self._content(request.blocks)}],
            "output_format": request.schema,
            **_thinking_for(request),
            **_sampling_for(request),
        }

    def _content(self, blocks: list[dict]) -> list[dict]:
        out = []
        for block in blocks:
            if block["type"] == "text":
                out.append({"type": "text", "text": block["text"]})
                continue
            if "stub" in block:
                # Showing, not sending: the label stands where the bytes go.
                out.append({"type": "image", "source": {
                    "type": "base64", "media_type": "image/jpeg", "data": block["stub"]}})
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

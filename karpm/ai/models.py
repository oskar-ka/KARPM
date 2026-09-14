"""The models a pass can be pointed at, and what each of them accepts.

One table, read by two places that would otherwise drift apart: the config page
builds its dropdown from it, and the provider decides from it whether to send
`thinking` and an effort level. The bug that made this a table was exactly that
drift - the form offered an effort level for a model that rejects the parameter,
and every listing in the queue came back a 400.

Prices are dollars per million tokens, as of the date below. They are here to
make the dropdown an informed choice, not to bill anything, so being a little
out of date is survivable - but say so rather than showing a stale number as
fact.
"""

from __future__ import annotations

from dataclasses import dataclass

PRICES_AS_OF = "2026-06-24"

EVERY_EFFORT = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class Model:
    id: str
    input_usd: float                    # per million tokens
    output_usd: float
    vision: bool = True
    # Takes `thinking: {"type": "adaptive"}` and `output_config.effort`. The
    # older generation takes a fixed `budget_tokens` instead, which is no use
    # to a reading pass - it would eat into the same `max_tokens` the findings
    # have to fit in - so those models are asked without either.
    thinking: bool = True
    effort: tuple[str, ...] = EVERY_EFFORT

    @property
    def label(self) -> str:
        return f"{self.id} - ${self.input_usd:g}/${self.output_usd:g} per Mtok"


CATALOGUE = (
    Model("claude-haiku-4-5", 1, 5, thinking=False, effort=()),
    Model("claude-sonnet-5", 2, 10),
    Model("claude-opus-5", 5, 25),
    Model("claude-fable-5-1", 10, 50),
)

BY_ID = {model.id: model for model in CATALOGUE}

# Cheapest first: the question this dropdown answers is usually "how little can
# I get away with for this pass".
IDS = tuple(model.id for model in CATALOGUE)
LABELS = {model.id: model.label for model in CATALOGUE}

# The models that take an effort level, for the config page to hide the row for
# the ones that do not.
WITH_EFFORT = tuple(model.id for model in CATALOGUE if model.effort)


def get(model_id: str) -> Model | None:
    """What is known about a model, or None for one not in the table.

    None is not an error. The config accepts a model typed into the file by
    hand, which is how you use one released after this table was written; the
    caller decides what to assume about it.
    """
    return BY_ID.get(model_id)

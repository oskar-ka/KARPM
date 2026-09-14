"""Three passes over an ad, each with its own model.

    text    reads the seller's prose
    photos  looks at the pictures and picks the ones worth keeping
    score   weighs it all up

They are separate calls because they are different jobs, because a different
model may suit each, and because they go stale for different reasons: an edited
description does not make the photographs wrong.
"""

from . import extract, passes, provider

__all__ = ["extract", "passes", "provider"]

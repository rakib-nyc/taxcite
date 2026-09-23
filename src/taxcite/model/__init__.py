"""Tax computations that are grounded in published authority.

Everything in this package computes; nothing recommends. A model here takes facts the
user supplies, applies a rule stated in the Code or the regulations, and returns the
arithmetic together with a citation for every input and every step. It does not
suggest a transaction, evaluate whether one is advisable, or opine on whether a
position would be sustained.

That boundary is the same one the rest of TaxCite keeps. The tool can tell you what
the law says a number is. It cannot tell you what to do about it.
"""

from __future__ import annotations

__all__ = ["section382"]

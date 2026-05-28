"""Graph-format adapters.

Currently exposes the graphology-JSON <-> NetworkX adapter used by the
notebook / interactive-viewer helpers. The graph query API emits
graphology serialized JSON; this package converts that wire shape into a
``networkx`` graph so viz tools (ipysigma, PyVis, ...) can consume it.

``networkx`` is an OPT-IN dependency (the ``graph-notebook`` extra), so it
is imported lazily inside the adapter functions — importing this package
never requires networkx to be installed.
"""

from movate.core.graph.networkx_format import (
    GraphFormatError,
    graphology_to_networkx,
)

__all__ = [
    "GraphFormatError",
    "graphology_to_networkx",
]

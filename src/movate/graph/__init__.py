"""Interactive knowledge-graph viewers (analyst / notebook surface).

This package hosts the data-science / Jupyter viewer option for exploring
the knowledge graph. :mod:`movate.graph.notebook` provides an ``ipysigma``
helper that renders a graphology/NetworkX graph as an interactive sigma.js
widget *inside a notebook*.

Everything here goes through the graph query API (no direct storage
access) and leans on OPT-IN dependencies (the ``graph-notebook`` extra:
``networkx`` + ``ipysigma``) imported lazily with a friendly install hint.
"""

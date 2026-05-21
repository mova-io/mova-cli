"""Shared utility helpers for movate internals.

Small, zero-dependency helpers that multiple sub-packages need.
Nothing here should import from ``movate`` core modules — this
package sits at the bottom of the import graph.
"""

from movate.utils.git import git_short_sha

__all__ = ["git_short_sha"]

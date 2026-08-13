"""Single source of truth for the VMSG version.

Previously the version was written out in two places and had already drifted
(the banner said 1.2.0 while the FastAPI app reported 1.1.0). Import it from
here instead of re-typing it.
"""

__version__ = "1.2.0"

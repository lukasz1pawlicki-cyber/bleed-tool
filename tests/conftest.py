"""Pytest config — dodaje katalog glowny do sys.path."""
import os
import sys

# Katalog nadrzedny (bleed-tool) na sys.path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

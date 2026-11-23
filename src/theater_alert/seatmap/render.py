'''Render seatmaps as images for Discord messages.'''

from __future__ import annotations

import io
from datetime import datetime

from PIL import Image, UnidentifiedImageError

from theater_alert.models import Event, Seatmap


def render_seatmap_image(_event: Event, seatmap: Seatmap, _checked_at: datetime) -> bytes:
    '''Return only the seatmap image, without added text or decorations.'''
    if seatmap.screenshot:
        try:
            image = Image.open(io.BytesIO(seatmap.screenshot))
            image.load()
            output = io.BytesIO()
            image.convert('RGB').save(output, format='PNG', optimize=True)
            return output.getvalue()
        except (UnidentifiedImageError, OSError, ValueError):
            pass
    return _blank_image()


def _blank_image() -> bytes:
    output = io.BytesIO()
    Image.new('RGB', (16, 16), '#151a20').save(output, format='PNG', optimize=True)
    return output.getvalue()

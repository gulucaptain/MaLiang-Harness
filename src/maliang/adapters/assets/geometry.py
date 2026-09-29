"""Coordinate helpers shared by inspection and placement."""

def background_geometry(width, height, target_width, target_height):
    """Centered cover crop: retain aspect ratio; never squash the generated plate."""
    scale = max(target_width / width, target_height / height)
    sw, sh = target_width / scale, target_height / scale
    return [(width - sw) / 2, (height - sh) / 2, sw, sh]

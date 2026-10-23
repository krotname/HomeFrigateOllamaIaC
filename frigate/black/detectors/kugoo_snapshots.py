"""Refresh a native two-wheeler snapshot without reusing an old frame."""
import math

from frigate.util.image import is_better_thumbnail as original_is_better_thumbnail
from frigate.util.image import on_edge


def native_context_regions(camera, turn, regions, tracked_boxes, frame_shape):
    """Add one bounded, native-resolution view without cutting a scooter in half."""
    if camera not in {"cam1_ds_i202", "common_camera03"}:
        return regions
    height, width = frame_shape[:2]
    side = min(960, width, height)
    tiles = [(x, y, x + side, y + side)
             for x in sorted({0, (width - side) // 2, width - side})
             for y in sorted({0, height - side})]
    selected = tiles[turn % len(tiles)]
    if tracked_boxes and turn % 4 != 3:
        box = max(tracked_boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        selected = min(tiles, key=lambda r: ((r[0] + r[2]) / 2 - cx) ** 2
                       + ((r[1] + r[3]) / 2 - cy) ** 2)
    return [tuple(region) for region in regions if tuple(region) != selected] + [selected]


def is_better_thumbnail(label_attributes, current_thumb, new_obj, frame_shape):
    if original_is_better_thumbnail(label_attributes, current_thumb, new_obj, frame_shape):
        return True
    if new_obj.get("label") not in {"bicycle", "motorcycle"}:
        return False
    try:
        age = float(new_obj["frame_time"]) - float(current_thumb["frame_time"])
        score = float(new_obj["score"])
        area = float(new_obj["area"])
        previous_area = float(current_thumb["area"])
        if not all(math.isfinite(value) for value in (age, score, area, previous_area)):
            return False
        # The worker reads the exact native JPEG immediately; a short track can
        # provide a second real frame without waiting for a recording segment.
        return (age >= 5 and score >= .7 and previous_area > 0
                and area >= previous_area * .9
                and not on_edge(new_obj["box"], frame_shape))
    except (KeyError, TypeError, ValueError):
        return False

"""Refresh a native two-wheeler snapshot without reusing an old frame."""
import math

from frigate.util.image import is_better_thumbnail as original_is_better_thumbnail
from frigate.util.image import on_edge


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
        return (age >= 5 and score >= .7 and previous_area > 0
                and area >= previous_area * .9
                and not on_edge(new_obj["box"], frame_shape))
    except (KeyError, TypeError, ValueError):
        return False

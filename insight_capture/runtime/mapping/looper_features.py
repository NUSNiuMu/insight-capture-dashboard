"""The single SuperPoint/SuperGlue frontend used by LooperVIO mapping."""
import numpy as np

from .stereo_refinement import refine_stereo_matches


def stereo_arrays(left, right, matches):
    refined = refine_stereo_matches(left, right, matches)
    a, b = refined.left_points, refined.right_points
    keep = (np.abs(a[:, 1] - b[:, 1]) <= 1.) & (a[:, 0] - b[:, 0] >= .5)
    return tuple(np.asarray(x, np.float32) for x in
                 (a[keep], b[keep], refined.descriptors[keep]))


def query_arrays(pixels, descriptors, image_height, mask_height_ratio):
    keep = pixels[:, 1] < image_height * (1. - mask_height_ratio)
    return (np.asarray(pixels[keep], np.float32),
            np.asarray(descriptors[keep], np.float32))

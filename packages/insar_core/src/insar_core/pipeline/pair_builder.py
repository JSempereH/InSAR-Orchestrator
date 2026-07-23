from __future__ import annotations

from typing import List, Tuple

from insar_core.models.scene import SARScene


def build_sbas_pairs(
    scenes: List[SARScene],
    max_temporal_neighbors: int = 3,
) -> List[Tuple[SARScene, SARScene]]:
    """Build SBAS interferometric pairs using a temporal-neighbor network.

    Each scene is paired with the next `max_temporal_neighbors` scenes in time.
    This is a standard small-baseline strategy: dense enough to maintain
    network connectivity, sparse enough to keep coherence high.

    Args:
        scenes: Scenes to pair, must all be from the same track/direction.
        max_temporal_neighbors: How many forward scenes each acquisition connects to.

    Returns:
        List of (reference, secondary) scene pairs, reference always earlier.
    """
    sorted_scenes = sorted(scenes, key=lambda s: s.acquisition_date)
    pairs: List[Tuple[SARScene, SARScene]] = []
    n = len(sorted_scenes)
    for i, ref in enumerate(sorted_scenes):
        upper = min(i + 1 + max_temporal_neighbors, n)
        for j in range(i + 1, upper):
            pairs.append((ref, sorted_scenes[j]))
    return pairs

"""Safety-adjacency precomputation for railway maintenance scheduling."""

from typing import Any, Union

from cadence.domain.graph import NetworkGraph
from cadence.domain.models import TrackSection
from cadence.domain.schemas import TrackSectionSchema
from cadence.profiles.base import NetworkProfile


def precompute_unsafe_adjacency_pairs(
    graph: NetworkGraph,
    profile: NetworkProfile,
    sections: list[Union[TrackSectionSchema, TrackSection]],
    **kwargs: Any,
) -> list[tuple[str, str, str]]:
    """Precompute all pairs of adjacent track sections that cannot be safely blocked simultaneously.

    Enumerates candidate adjacent pairs via graph.get_neighbors to avoid O(N^2) brute-forcing,
    calls the active NetworkProfile's is_safe_adjacency() method, and returns the list of
    flagged unsafe pairs along with their explanatory reason strings.

    Args:
        graph: NetworkGraph representation of the track network.
        profile: Active NetworkProfile defining safety adjacency rules.
        sections: Track sections in the network (TrackSectionSchema or TrackSection models).
        **kwargs: Additional contextual keyword arguments passed to is_safe_adjacency
            (e.g., train_slots for LocalProfile).

    Returns:
        list[tuple[str, str, str]]: List of (section_a_id, section_b_id, reason) tuples
            for every adjacent pair deemed unsafe to block simultaneously.
    """
    if graph is None or not sections:
        return []

    seen_pairs: set[tuple[str, str]] = set()
    unsafe_pairs: list[tuple[str, str, str]] = []

    for section in sections:
        sec_id = section.id if hasattr(section, "id") else section["id"]
        neighbors = graph.get_neighbors(sec_id)

        for neighbor_id in neighbors:
            if sec_id == neighbor_id:
                continue

            pair_key = tuple(sorted((sec_id, neighbor_id)))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            sec_a, sec_b = pair_key
            is_safe, reason = profile.is_safe_adjacency(
                graph=graph,
                section_a_id=sec_a,
                section_b_id=sec_b,
                currently_blocked=set(),
                **kwargs,
            )

            if not is_safe:
                unsafe_pairs.append((sec_a, sec_b, reason))

    return unsafe_pairs


def count_safety_violations(
    scheduled_blocks: list[Union[Any, TrackSectionSchema]],
    unsafe_pairs: list[tuple[str, str, ...]],
) -> int:
    """Scan a solved schedule and mechanically count any actual overlap between blocks on flagged-unsafe section pairs.

    Args:
        scheduled_blocks: List of ScheduledBlockSchema or block objects with section_id,
            start_time, end_time.
        unsafe_pairs: List of tuples (section_a_id, section_b_id, ...) flagged unsafe.

    Returns:
        int: Number of pairwise temporal overlaps on unsafe adjacent section pairs.
    """
    unsafe_set: set[tuple[str, str]] = set()
    for pair in unsafe_pairs:
        u = pair[0]
        v = pair[1]
        unsafe_set.add(tuple(sorted((u, v))))

    violations = 0
    n = len(scheduled_blocks)
    for i in range(n):
        b1 = scheduled_blocks[i]
        sec1 = b1.section_id if hasattr(b1, "section_id") else b1["section_id"]
        start1 = b1.start_time if hasattr(b1, "start_time") else b1["start_time"]
        end1 = b1.end_time if hasattr(b1, "end_time") else b1["end_time"]

        for j in range(i + 1, n):
            b2 = scheduled_blocks[j]
            sec2 = b2.section_id if hasattr(b2, "section_id") else b2["section_id"]

            pair_key = tuple(sorted((sec1, sec2)))
            if pair_key in unsafe_set:
                start2 = b2.start_time if hasattr(b2, "start_time") else b2["start_time"]
                end2 = b2.end_time if hasattr(b2, "end_time") else b2["end_time"]

                # Overlap exists iff max(start1, start2) < min(end1, end2)
                if max(start1, start2) < min(end1, end2):
                    violations += 1

    return violations

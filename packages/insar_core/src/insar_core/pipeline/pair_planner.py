"""Choose which interferograms to make, and say what that choice costs and leaves out.

Every pair sent to HyP3 costs credits, and the set of pairs decides how good the time series can
be: too sparse and one bad interferogram breaks the chain, too dense and the credits go to pairs
that add little. This module builds the pair network and reports what a person needs to judge it:
the number of pairs and credits, whether the network is connected, how many interferograms each
date has, and how many pairs exceed a perpendicular baseline limit.

Two strategies:

* ``neighbors``: each date is paired with the next N dates. Optionally also with one date about
  three months later and one about a year later (``long_pairs_days``, off by default), and with one
  pair over the whole period (``calibration_pair``) that fixes the constants of the fading bias
  correction. It never drops a pair for its baseline, it only reports how many are over the limit.

  The long pairs were meant against the fading bias. An interferogram of a few days over soil or
  vegetation carries a small phase that is not motion, always of the same sign, and a network made
  only of such interferograms adds it up into millimetres per year of false motion (Ansari et al.,
  2021; measured on real stacks by insar_core.analysis.fading_bias). That phase fades with the time
  an interferogram spans, so a pair of months or a year hardly has it, and in the inversion it
  pulls the series back. It helps where the ground keeps some coherence that long. Where it does
  not, the long pair is mostly noise, the weighted inversion gives it little say, and the credits
  it cost bought little. They are off by default because over ground that moves fast they do harm:
  a long interferogram aliases there, unwrapping loses whole cycles and the motion comes out
  smaller (in a subsiding basin the velocity at a GNSS station in the bowl went from 41% of the
  station's to 22% with 3-month pairs). The literature does not use long pairs as the standard
  cure either: it estimates the phases from all the interferograms (phase linking) or corrects the
  short ones from their loop closures (see ``analysis.fading_correction``).
* ``baseline``: pairs are chosen by temporal spacing (multiples of the revisit interval, or the
  spacing you give) and perpendicular baseline, then every date is brought to between ``min_degree``
  and ``max_degree`` interferograms and the network is checked for connectivity. The degree rules
  follow the approach of InSARHub (MIT licence): fill the dates that fall short with their nearest
  neighbours in time, trim the worst pairs of the dates that have too many.
"""
from __future__ import annotations

import itertools
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional, Sequence

from insar_core.models.scene import SARScene

CREDITS_PER_PAIR = 10          # HyP3 INSAR_GAMMA at 80 m (20x4 looks)
LONG_PAIRS_DAYS = ()           # off: see the note on the neighbors strategy. (90, 365) is about three months and a year.
STRATEGIES = ("neighbors", "baseline")


@dataclass
class PairPlanConfig:
    strategy: str = "neighbors"
    max_temporal_neighbors: int = 3
    long_pairs_days: Sequence[int] = LONG_PAIRS_DAYS    # neighbors strategy: also pair each date with one about this much later
    calibration_pair: bool = False                      # neighbors strategy: one pair over the whole period, for the fading correction
    # baseline strategy
    dt_targets: Optional[Sequence[int]] = None   # days; None means 1 to 4 times the median revisit interval
    dt_tolerance: int = 3                        # days either side of a target
    dt_max: Optional[int] = None                 # days; None means 4 times the median revisit interval
    pb_max: Optional[float] = 150.0              # metres; None means no limit
    min_degree: int = 2
    max_degree: int = 5
    include_annual: bool = False                 # also pair dates about a year apart

    def validate(self) -> None:
        if self.strategy not in STRATEGIES:
            raise ValueError(f"strategy must be one of {', '.join(STRATEGIES)}")
        if self.max_temporal_neighbors < 1:
            raise ValueError("max_temporal_neighbors must be at least 1")
        if any(int(days) < 1 for days in self.long_pairs_days):
            raise ValueError("long_pairs_days must be whole numbers of days, at least 1")
        if self.min_degree < 1 or self.max_degree < self.min_degree:
            raise ValueError("min_degree must be at least 1 and no larger than max_degree")
        if self.dt_tolerance < 0 or (self.dt_max is not None and self.dt_max < 1):
            raise ValueError("dt_tolerance cannot be negative and dt_max must be at least 1 day")
        if self.pb_max is not None and self.pb_max <= 0:
            raise ValueError("pb_max must be above 0 metres, or empty for no limit")

    def as_record(self) -> dict:
        """What is saved with a batch so the choice can be reproduced."""
        record: dict = {"strategy": self.strategy, "max_temporal_neighbors": self.max_temporal_neighbors}
        if self.strategy == "neighbors":
            record["long_pairs_days"] = [int(d) for d in self.long_pairs_days]
            record["calibration_pair"] = bool(self.calibration_pair)
        if self.strategy == "baseline":
            record.update({
                "dt_targets": list(self.dt_targets) if self.dt_targets else None, "dt_tolerance": self.dt_tolerance,
                "dt_max": self.dt_max, "pb_max": self.pb_max, "min_degree": self.min_degree,
                "max_degree": self.max_degree, "include_annual": self.include_annual,
            })
        return record


@dataclass
class PlannedPair:
    reference: SARScene
    secondary: SARScene
    temporal_days: int
    perpendicular_m: Optional[float]       # absolute value, None when a baseline is unknown
    over_limit: bool = False               # beyond pb_max (neighbors strategy keeps such pairs)
    reason: str = ""                       # why the pair is in the network
    state: str = "new"                     # "new", "submitted" (already sent to HyP3) or "excluded" (left out by hand)


@dataclass
class PairPlan:
    pairs: list[PlannedPair]
    scenes: list[SARScene]
    config: PairPlanConfig
    components: int = 1
    degrees: dict[date, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    credits_per_pair: float = CREDITS_PER_PAIR      # a whole scene at 80 m; a burst pair costs less (asf_burst.burst_credits)

    @property
    def new_pairs(self) -> list[PlannedPair]:
        return [p for p in self.pairs if p.state == "new"]

    @property
    def credits(self) -> int:
        """Credits the pairs still to submit will cost. Pairs already sent or left out cost nothing."""
        return int(round(len(self.new_pairs) * self.credits_per_pair))

    def mark(self, submitted: Iterable[tuple[str, str]] = (), excluded: Iterable[tuple[str, str]] = ()) -> "PairPlan":
        """Label pairs that were already submitted or that the person left out, by granule names.

        The network keeps them: connectivity is judged on every pair that exists or will exist, so an
        update plans only what is missing without losing sight of what is already there."""
        submitted, excluded = set(submitted), set(excluded)
        for pair in self.pairs:
            key = (pair.reference.granule_name, pair.secondary.granule_name)
            pair.state = "submitted" if key in submitted else "excluded" if key in excluded else "new"
        return self

    @property
    def over_limit(self) -> int:
        return sum(1 for p in self.pairs if p.over_limit)

    @property
    def connected(self) -> bool:
        return self.components <= 1

    def as_pairs(self) -> list[tuple[SARScene, SARScene]]:
        """The pairs still to submit, as (reference, secondary)."""
        return [(p.reference, p.secondary) for p in self.new_pairs]


def _bperp(a: SARScene, b: SARScene) -> Optional[float]:
    if a.perpendicular_baseline_m is None or b.perpendicular_baseline_m is None:
        return None
    return abs(b.perpendicular_baseline_m - a.perpendicular_baseline_m)


def _days(a: SARScene, b: SARScene) -> int:
    return abs((b.acquisition_date - a.acquisition_date).days)


def _components(scenes: Sequence[SARScene], pairs: Iterable[tuple[SARScene, SARScene]]) -> tuple[int, dict[date, int]]:
    parent = {s.acquisition_date: s.acquisition_date for s in scenes}

    def find(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    degrees = {s.acquisition_date: 0 for s in scenes}
    for a, b in pairs:
        parent[find(a.acquisition_date)] = find(b.acquisition_date)
        degrees[a.acquisition_date] += 1
        degrees[b.acquisition_date] += 1
    return len({find(d) for d in parent}), degrees


def _neighbors(scenes: Sequence[SARScene], config: PairPlanConfig) -> list[tuple[SARScene, SARScene, str]]:
    out = []
    for i, ref in enumerate(scenes):
        for sec in scenes[i + 1:i + 1 + config.max_temporal_neighbors]:
            out.append((ref, sec, f"one of the next {config.max_temporal_neighbors} acquisitions"))
    if config.calibration_pair and len(scenes) > 6:
        # The correction needs a pair from the first date to a date a whole number of 6-step chains away, over which the
        # 1, 2 and 3-step pairs also run: the last such date.
        end = 6 * ((len(scenes) - 1) // 6)
        out.append((scenes[0], scenes[end], "one pair over the whole period, to fix the constants of the fading bias correction"))
    if not config.long_pairs_days:
        return out
    # One later date per span: among those within a revisit of it, the one with the smallest perpendicular
    # baseline, because a long pair needs all the coherence it can keep.
    step = _revisit_days(scenes)
    have = {(a.acquisition_date, b.acquisition_date) for a, b, _ in out}
    for i, ref in enumerate(scenes):
        for span in sorted({int(d) for d in config.long_pairs_days}):
            near = [s for s in scenes[i + 1:] if abs(_days(ref, s) - span) <= step]
            if not near:
                continue
            sec = min(near, key=lambda s: (_bperp(ref, s) if _bperp(ref, s) is not None else float("inf"), abs(_days(ref, s) - span)))
            if (ref.acquisition_date, sec.acquisition_date) not in have:
                have.add((ref.acquisition_date, sec.acquisition_date))
                out.append((ref, sec, f"about {span} days apart, against the fading bias of short pairs"))
    return out


def _revisit_days(scenes: Sequence[SARScene]) -> int:
    gaps = [(b.acquisition_date - a.acquisition_date).days for a, b in zip(scenes, scenes[1:], strict=False)]
    return max(1, int(statistics.median(gaps))) if gaps else 12


def _by_baseline(scenes: Sequence[SARScene], config: PairPlanConfig, warnings: list[str]) -> list[tuple[SARScene, SARScene, str]]:
    step = _revisit_days(scenes)
    targets = list(config.dt_targets) if config.dt_targets else [k * step for k in (1, 2, 3, 4)]
    # Without a stated limit the longest target sets it: a target the person gave is never dropped in silence.
    dt_max = config.dt_max if config.dt_max is not None else max(targets) + config.dt_tolerance
    out_of_reach = [t for t in targets if t - config.dt_tolerance > dt_max]
    if out_of_reach:
        warnings.append(f"Temporal spacing target(s) {', '.join(str(t) for t in out_of_reach)} days are longer than the "
                        f"{dt_max}-day limit and give no pairs.")
    # A year is not a whole number of revisits (365 days is 30.4 twelve-day cycles), so aim at the nearest one.
    annual = (max(step, round(365 / step) * step),) if config.include_annual else ()

    def allowed(a: SARScene, b: SARScene) -> bool:
        if _days(a, b) > max(dt_max, annual[0] + config.dt_tolerance if annual else 0):
            return False
        bp = _bperp(a, b)
        return bp is None or config.pb_max is None or bp <= config.pb_max

    def near(a: SARScene, b: SARScene) -> Optional[str]:
        days = _days(a, b)
        if days <= dt_max and any(abs(days - t) <= config.dt_tolerance for t in targets):
            return "temporal spacing near a target"
        if annual and abs(days - annual[0]) <= config.dt_tolerance:
            return "about a year apart"
        return None

    chosen: dict[tuple[date, date], str] = {}
    for a, b in itertools.combinations(scenes, 2):
        reason = near(a, b)
        if reason and allowed(a, b):
            chosen[(a.acquisition_date, b.acquisition_date)] = reason
    by_date = {s.acquisition_date: s for s in scenes}
    neighbours: dict[date, set[date]] = {s.acquisition_date: set() for s in scenes}
    for a, b in chosen:
        neighbours[a].add(b)
        neighbours[b].add(a)

    def ordered(x: date, y: date) -> tuple[date, date]:
        return (x, y) if x <= y else (y, x)

    # Raise the dates that have too few interferograms, using their nearest dates in time.
    for scene in scenes:
        d = scene.acquisition_date
        if len(neighbours[d]) >= config.min_degree:
            continue
        for other in sorted((s for s in scenes if s is not scene), key=lambda s: abs((s.acquisition_date - d).days)):
            if len(neighbours[d]) >= config.min_degree:
                break
            o = other.acquisition_date
            if o in neighbours[d] or _days(scene, other) > max(dt_max, 2 * step * config.min_degree):
                continue
            if not allowed(scene, other):
                continue
            chosen[ordered(d, o)] = "added so the date has enough interferograms"
            neighbours[d].add(o)
            neighbours[o].add(d)
        if len(neighbours[d]) < config.min_degree:
            warnings.append(f"{d.isoformat()} has only {len(neighbours[d])} interferogram(s) within the limits")

    # Trim the dates that have too many, worst pair first (longest in time, then largest baseline).
    for scene in scenes:
        d = scene.acquisition_date
        while len(neighbours[d]) > config.max_degree:
            ranked = sorted(
                neighbours[d],
                key=lambda o: (_days(scene, by_date[o]), _bperp(scene, by_date[o]) or 0.0),
                reverse=True,
            )
            drop = next((o for o in ranked if len(neighbours[o]) > config.min_degree and chosen.get(ordered(d, o)) != "about a year apart"), None)
            if drop is None:
                break
            chosen.pop(ordered(d, drop), None)
            neighbours[d].discard(drop)
            neighbours[drop].discard(d)

    return [(by_date[a], by_date[b], reason) for (a, b), reason in sorted(chosen.items())]


def _bridge(scenes: Sequence[SARScene], pairs: list[tuple[SARScene, SARScene, str]], warnings: list[str]) -> list[tuple[SARScene, SARScene, str]]:
    """Join separate groups of dates with the closest pair across each gap, and say so."""
    pairs = list(pairs)
    while True:
        count, _ = _components(scenes, [(a, b) for a, b, _ in pairs])
        if count <= 1:
            return pairs
        parent = {s.acquisition_date: s.acquisition_date for s in scenes}

        def find(item, parent=parent):
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item

        for a, b, _ in pairs:
            parent[find(a.acquisition_date)] = find(b.acquisition_date)
        best = min(
            ((a, b) for a, b in itertools.combinations(scenes, 2) if find(a.acquisition_date) != find(b.acquisition_date)),
            key=lambda ab: (_days(*ab), _bperp(*ab) or 0.0),
        )
        pairs.append((best[0], best[1], "added to connect two separate groups of dates"))
        warnings.append(
            f"Added {best[0].acquisition_date.isoformat()} to {best[1].acquisition_date.isoformat()} "
            f"({_days(*best)} days) to connect the network; it may break the limits you set.")


def plan_pairs(scenes: Sequence[SARScene], config: Optional[PairPlanConfig] = None,
               credits_per_pair: float = CREDITS_PER_PAIR) -> PairPlan:
    """The pair network for `scenes` (one acquisition per date, one track) under `config`.

    `credits_per_pair` is what one interferogram costs, for the total the plan reports."""
    config = config or PairPlanConfig()
    config.validate()
    ordered_scenes = sorted(scenes, key=lambda s: s.acquisition_date)
    warnings: list[str] = []
    if len(ordered_scenes) < 2:
        return PairPlan([], list(ordered_scenes), config, components=len(ordered_scenes),
                        warnings=["Fewer than two acquisitions: no pair can be made."], credits_per_pair=credits_per_pair)
    if sum(1 for s in ordered_scenes if s.perpendicular_baseline_m is None):
        warnings.append("Some scenes have no perpendicular baseline (ASF returned no orbit data), so it is not applied to them.")

    raw = _neighbors(ordered_scenes, config) if config.strategy == "neighbors" else _by_baseline(ordered_scenes, config, warnings)
    if config.strategy == "baseline":
        raw = _bridge(ordered_scenes, raw, warnings)

    planned = []
    for ref, sec, reason in raw:
        bp = _bperp(ref, sec)
        planned.append(PlannedPair(
            reference=ref, secondary=sec, temporal_days=_days(ref, sec), perpendicular_m=bp,
            over_limit=bool(config.pb_max is not None and bp is not None and bp > config.pb_max), reason=reason,
        ))
    components, degrees = _components(ordered_scenes, [(p.reference, p.secondary) for p in planned])
    plan = PairPlan(planned, ordered_scenes, config, components=components, degrees=degrees, warnings=warnings,
                    credits_per_pair=credits_per_pair)
    if not plan.connected:
        plan.warnings.append(f"The network splits into {plan.components} groups of dates that nothing ties together.")
    weak = [d for d, k in degrees.items() if k < 2]
    if weak and len(ordered_scenes) > 2:
        plan.warnings.append(f"{len(weak)} date(s) have a single interferogram, so an error in it goes straight into the series.")
    return plan

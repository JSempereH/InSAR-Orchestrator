# Known limits

What this chain does not do, in one place, so a result is read for what it is. Each point says what happens, how the app tells you, and what to do.

## What the result is

- **SBAS at 80 m, not persistent scatterers.** HyP3 whole scenes are multilooked to 80 m (bursts to 80, 40 or 20 m) and MintPy inverts a small-baseline network. One pixel mixes buildings, streets and empty lots. The result suits motion of the scale of a neighbourhood or larger (basins, mining, large works, landslides), not the motion of one structure. For that, EGMS (persistent and distributed scatterers at full resolution) is the reference.
- **One line of sight.** A single track measures the motion along its line of sight, which mixes vertical and horizontal motion. The words in the app say *towards* and *away from the satellite* for that reason. Vertical and east-west motion need an ascending and a descending track over the same period (see [Vertical and east-west](vertical-east-west.md)).
- **Relative to a reference pixel.** Every velocity is relative to one pixel. If the reference moves (a whole basin subsiding), everything else is shifted. GNSS in the area ties the map to an absolute frame (see [Check and export](check-and-export.md)); without it the map is relative.
- **A straight line.** The velocity is the slope of a line through the series. A basin that does not subside at a constant rate gets an average. The series itself, shown in the app, is the better description.

## What is detected but not corrected

- **Fading bias.** Interferograms of a few days over soil or vegetation carry a small phase with the same sign, which a network of short pairs adds up into millimetres per year (about -2 mm/year in the median, more where coherence is low, in the test area). The app measures it from the loop closures and flags it. A correction from the loop closures (Maghsoudi and others, 2022) is available but off by default, because in the one basin checked against GNSS and EGMS it moved the velocity away from both. Long pairs, the other mitigation, lose fast motion. What is measured is a lower bound. The reference method in the literature, phase linking, needs complex images that this chain does not have.
- **Fast motion.** Where the ground moves faster than a few centimetres a year, long interferograms alias, and even short ones lose part of it at 80 m. In the test basin the velocity at a GNSS station in the bowl was 20% to 40% of the station's. The check *Fast motion in long interferograms* flags it; treat velocities there as a lower bound.
- **Unwrapping errors.** Whole-cycle errors are found from the closure of triplets of short interferograms and corrected where the answer is unambiguous (about 98% of injected errors are recovered; about 1% of the pixels of a real scene had them). Errors in long interferograms are not judged, because their triplets are noisy by construction.

## What is not modelled

- **Atmosphere.** Only the ERA5 weather model (a grid of about 25 km, no login needed) is removed. The turbulent atmosphere that dominates at scales of kilometres stays: with a year of data the uncertainty of the velocity reaches about 3 mm/year at 10 km from the reference (see *Precision with distance*). The precision is measured from the data and reported, but it is precision, not accuracy.
- **Ionosphere.** Not corrected in Sentinel-1.
- **Soil moisture and seasons.** Moisture changes the phase of interferograms and in a year of data mixes with the seasonal cycle. The seasonal term of the time series analysis helps in the window, not in the map.
- **Layover and shadow.** There is no geometry mask for steep slopes and tall buildings.
- **Thermal expansion** of structures is a real seasonal signal that a line reads as velocity.

## What the checks need

- **EGMS** covers until 2024 and **GNSS** has few stations, so there is often no independent source for the period. A stable zone of known zero motion is the best control for the background noise.
- **Comparison with EGMS** uses the median of the EGMS points inside each 80 m pixel. In a city a pixel mixes points that move differently, so part of the disagreement is representation, not error.
- **Short series.** A velocity from less than six months is extrapolated to a year; the app warns.

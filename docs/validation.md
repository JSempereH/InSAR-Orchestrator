# Validating the chain

What a result is worth is only known by comparing it with something independent. This page says what the app is checked against, how to repeat the checks, and what is still missing before the chain can be trusted in a new kind of place.

## What is checked, and against what

| Case | Ground | Independent data | What it tests |
|---|---|---|---|
| `lorca-desc-2023` | farmland and a fast-subsiding basin, descending track 8, 2023 | EGMS L2b (34,600 pixels) and the GNSS stations LOR1 and LRC1 | the chain with short and long pairs; the bowl is the hardest case |
| `lorca-desc-2023-short-pairs` | the same, only the short pairs (up to 36 days) | the same | the network the app uses by default |
| `lorca-asc-2023` | the same basin, ascending track | GNSS | the other side of the basin |
| `madrid-asc-2023` | a city, ascending track 1, 2023 | EGMS L2b tile 001 and GNSS if a station is inside | a coherent urban area |
| `valladolid-default-2023` | slow farmland that does not move, ascending track 1, 2023, with a pair a year long | EGMS L2b (84,000 pixels) and the GNSS stations PEN1 and ARDU | the control: any velocity found is error |
| `valladolid-fading-2023` | the same with the correction of the fading bias on | the same | whether that correction helps |
| `valladolid-short-2023` | the same, only the short pairs (up to 36 days) | the same | the network the app uses by default |
| `valladolid-short-deramp-2023` | the same with a linear ramp removed from every date | the same | the remedy for the error found |

The first three are the same ground seen in different ways, so they say little about how the chain behaves on other terrain, and the fourth has no motion to find. The Valladolid cases are one more ground (slow farmland) seen four ways. **There is still no case of forest, of open desert or of a stable rock area, and none from a descending track over ground that does not move.** Those are what the next cases should be (see the end).

## How to repeat it

The cases are in `backend/benchmark/cases.json`: a finished MintPy folder plus the EGMS files and whether to look for GNSS stations. From `backend/`:

```
python -m app.benchmark                                        # run every case and print the numbers
python -m app.benchmark --only lorca-desc-2023-short-pairs     # one case
python -m app.benchmark --baseline benchmark/baseline.json     # compare with the saved numbers; exit code 1 if one moved
python -m app.benchmark --update-baseline benchmark/baseline.json   # accept today's numbers
```

`make benchmark` does the comparison. It needs the real folders (they are in `backend/downloads/`, not in the repository) and takes some minutes: reading the EGMS tiles is most of it. The GNSS part downloads the station series from the Nevada Geodetic Laboratory; `--no-gnss` leaves it out.

For each case it measures:

- **Against EGMS:** the spread of the difference in velocity (RMS and the robust MAD), the correlation, the slope of one on the other, and the mean difference by band of velocity. The band from -3 to 3 mm/year is the noise of the chain over ground that does not move; the fast bands say what it loses.
- **Against GNSS:** the velocity at each station over the period it recorded, projected onto the line of sight.
- **The run itself:** the status of every quality check, the uncertainty it states at 10 km, and what the chain did to this result (which corrections ran, how many interferograms were used and the longest).

A number counts as having moved when it changes by more than a tolerance (for example 0.30 mm/year in the RMS, 0.02 in the correlation, 0.6 mm/year in the bias of a band, 3 mm/year in the velocity at a GNSS station), and a quality check counts as having moved when its status changes. The tolerances are in `app/benchmark.py`. They are about the size of the difference between two runs of the same data; a smaller change is not told apart from chance.

Run it **before and after any change to the processing**, and keep the baseline in step with what you accept.

## What the numbers are today

See `backend/benchmark/baseline.json` for the exact figures (the baseline of 23 November 2026). What they say:

- **Farmland and a subsiding basin, descending track (Lorca).** Against EGMS the RMS of the difference in velocity is 3.7 mm/year (short and long pairs) to 4.1 (short pairs only), with a correlation of 0.90 to 0.92 over velocities from +10 to -40 mm/year. Over ground that does not move (the band from -3 to 3 mm/year) the RMS is 3.0 to 3.6. The chain underestimates subsidence: the slope on EGMS is 0.86 to 0.93, and the mean difference grows with the velocity (+0.4 to +1.8 mm/year in the subsiding bands with short pairs, +2.3 to +3.0 with the long ones).
- **The same basin, ascending track.** The station in the bowl (LOR1) reads -45.3 mm/year against -42.7 from GNSS, so the chain **does** recover the bowl from this side. From the descending track it reads -26.7 (short pairs) or -14.1 (with long pairs) against -64.9. The loss of fast motion in the bowl is therefore not the same on both tracks, and it is not known why (the acquisition time, 06:00 or 18:00, and the geometry both differ). The slow station (LRC1) is off by 7 mm/year from the ascending track and by 0.4 to 2.4 from the descending one, inside the 3 to 4 mm/year uncertainty of one station.
- **A city (Madrid, ascending).** The chain gives 1.5 mm/year RMS against EGMS on the 888 pixels that hold three or more EGMS points, all of them in the band that does not move, so the correlation (0.11) says nothing: there is no signal to correlate. What it shows is a noise floor of about 1.5 mm/year over an urban area, and quality checks that are all in order. No GNSS station recorded inside the area.
- **What MintPy states.** At the median pixel the difference from the reference pixel is 0.7 to 1.0 times the error MintPy states, so for a pixel next to its reference the stated error is about right. It is the growth with distance, which MintPy does not state, that the precision curve measures (3 mm/year at 10 km in the basin).

- **Slow farmland (Valladolid, ascending), the control.** The ground does not move: averaged over 2 km blocks EGMS varies by 0.9 mm/year. The chain as it comes gives 6.8 mm/year RMS against EGMS (short pairs) and has no correlation with it (-0.004), and at the two GNSS stations it is off by 7 and 23 mm/year. **This is the largest error of any case, and it is not the fading bias.** The correction of the fading bias did not help (7.6 mm/year, and PEN1 went from +0.8 to -8.2), and the pair a year long made it worse by aliasing (the `steep_phase` check fails). The error is a different plane on every date, 10 to 30 mm from one side of the 50 by 28 km area to the other, which does not follow the height of the ground (r = -0.03), so it is orbit error and large scale atmosphere or ionosphere, not the stratified troposphere that ERA5 corrects. Over a year the planes add up to a false slope that explains about half of the error. Removing a linear ramp from every date (`mintpy.deramp = linear`) takes the RMS to 5.1 mm/year (3.2 in the robust MAD), the correlation to 0.13, and the two stations to +2.2 against +1.5 (PEN1) and -0.9 against +9.1 (ARDU). What is left, mostly at ARDU, is not a plane, and the floor set by the noise of EGMS itself is about 3.3 mm/year. The ramp is **not** removed by default, because a plane is also what motion over the whole area looks like and removing it would delete it (Lorca). The check `date_ramps` says when the dates carry planes of 10 mm or more, and the option is there for ground known to be stable.

## What is still missing

1. **Forest, and farmland from a second track**, with EGMS or GNSS. Valladolid answered the question of the fading correction for farmland (it does not help, so it stays off by default) but found a larger error instead: the planes on every date. Whether a correction of the ionosphere (the split-spectrum products are not part of HyP3 INSAR_GAMMA) or of the troposphere with a model that ERA5 does not give (GACOS) removes what a ramp leaves is not known. A year of one track costs about 90 pairs, 900 credits at 80 m.
2. **A stable area as a control** in every project (rock, an airport, a flat desert), to measure the noise without motion directly, instead of reading it from the EGMS band near zero.
3. **A fast, localised motion of a different kind** (a landslide, a mine, a volcano), and **why the descending track loses the bowl and the ascending one does not**: to see whether the loss of fast motion in Lorca is the chain or the place.
4. **A second period** for the same ground, to see whether the numbers repeat.
5. **A comparison with another processing of the same interferograms** (for example ARIA or a phase-linking result where there is one), which separates what is the data from what is this chain.
6. **Synthetic truth.** Pieces of the chain are tested against data with a known answer (the unwrapping correction, the precision curve, the fading correction), but the chain from interferograms to velocity has not been run on a simulated stack with deformation, atmosphere and fading together.

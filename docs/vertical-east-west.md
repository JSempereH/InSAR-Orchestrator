# Vertical and east-west motion

A single track measures motion along the satellite's **line of sight**, which mixes vertical and horizontal movement. A building that sinks and a hillside that slides west can give the same number. Two tracks that look at the same ground from opposite sides can be told apart: the ascending pass looks from the west and the descending pass from the east, so vertical motion shows with the same sign in both and east-west motion with opposite signs.

The **Combine** page of the sidebar does that separation for two projects of the same area, one ascending and one descending, and checks the result against EGMS and GNSS.

## What you need

- Two projects over the same area, one **ascending** and one **descending**, both with MintPy finished (see [Get data](get-data.md)). Their dates do not have to match: the separation is made on the velocity of each, not on the series.
- Optional, to check the result: an **EGMS L3** download of the ORTHO-UP and ORTHO-EAST products over the area (see [Get data](get-data.md)), and internet access for the GNSS stations.

Choose the two projects on the **Combine** page and press **Separate**. It takes a few seconds. The order you choose them in does not matter.

## What the app does, and what it assumes

1. The descending map is put on the grid of the ascending one, skipping pixels either run has masked.
2. Each MintPy result is relative to its own reference pixel, and the two are not the same place. Both maps are shifted so that a small window around **one common point** reads zero. By default that point is the ascending project's MintPy reference point, or the descending one's when the first has no valid pixels in both maps. This assumes the ground does not move, vertically or east-west, at that point.
3. In every pixel the two line-of-sight values are written with that pixel's own incidence and azimuth, and the 2 x 2 system for the vertical and east-west velocity is solved.
4. **North motion is taken as zero.** Sentinel-1 flies nearly north-south and looks nearly east-west, so it is almost blind to north motion, but not entirely. A uniform north motion cancels with the common reference. What leaks is north motion that differs across the area, and the page states how much of a difference of 1 mm/year shows up in each result.
5. The standard error of each component is carried through the same system from the error MintPy reports for each line of sight. Where the two lines of sight are too alike to carry two independent pieces of information (condition number above 8) the pixel is masked.

Every file the app writes says these assumptions inside it.

## Reading the page

| Figure | Meaning |
|---|---|
| **Pixels** | Pixels with a value in both tracks that pass the conditioning test. |
| **Vertical error, east-west error** | The median standard error, propagated from MintPy's. It is optimistic: see [how far to trust the stated error](check-and-export.md#how-far-to-trust-the-stated-error). The east-west error is larger than the vertical one, because both lines of sight are mostly vertical. |
| **Conditioning** | How independent the two lines of sight are. About 1 is ideal. |
| **Reference** | Where both maps read zero. |

In the preview, red is down or west and green is up or east.

## Check it

### Against EGMS L3

EGMS publishes the vertical and east-west components as well. The app averages your map onto EGMS's 100 m grid, so that each compared value covers the same ground, removes the common offset (where each source puts its zero) and reports the error, correlation and slope, the error by how fast the ground moves, and whether the stated error is realistic.

EGMS L3 is a mean over the years of its release, and your map covers the window of your projects. Ground that changes speed between the two shows up as a difference that is not an error of either. The page says so with the result.

### Against GNSS

GNSS gives the three components directly, so it checks the separation without a projection. The vertical velocity is absolute. The horizontal one is relative to the stations' average motion: the plate motion all stations share is removed first. The check also estimates the shift that would tie each map to the stations and, with two or more stations, tests it on stations it has not seen (see [tie the map to GNSS](check-and-export.md#tie-the-map-to-gnss)).

With one station there is nothing to test against, and the page says the vertical result is only indicative. The east-west calibration needs at least two stations and is not made with one.

## Export

GeoTIFF files in millimetres per year, vertical up and east positive, cloud optimised: **Vertical**, **East-west** and the error of each. Once a check justifies them, **tied to GNSS** and **realistic error** versions appear. From a script: `GET /api/decompositions/<id>/export?layer=vertical`, with the layers `vertical`, `east`, `vertical_std`, `east_std` and the same four with `_calibrated`.

## Limits

- It separates two components out of two measurements, so any error in one track goes into both results, and the result is only as good as the two runs behind it.
- North motion is assumed zero. In an area where the ground moves mostly north-south, the result is wrong in a way that no check inside this page can show.
- If either project runs MintPy again, the products describe a result that is gone, and the page says so until you separate them again.

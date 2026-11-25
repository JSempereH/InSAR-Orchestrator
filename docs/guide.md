# Guide

[![The pipeline, step by step](images/pipeline.gif)](images/pipeline.mp4)

InSAR Orchestrator turns a polygon on a map into a ground-motion time series. Read in this order:

1. **[Quick start](quick-start.md)**: install, start, first steps and fixes for common problems.
2. **[Credentials](credentials.md)**: Earthdata, EGMS and the Copernicus climate login.
3. **[Get ground-motion data: ASF or EGMS](get-data.md)**: the two ways to get data, restarts and how a result was produced.
4. **[Check and export a result](check-and-export.md)**: MintPy options, quality checks, validation against EGMS or GNSS, and export to GeoTIFF, NetCDF, CSV and GeoPackage.
5. **[Vertical and east-west motion](vertical-east-west.md)**: separate an ascending and a descending project into vertical and east-west velocity, and check it.
6. **[Reading a time series](time-series-analysis.md)**: what the numbers in the viewer mean.
7. **[Known limits](limits.md)**: what the chain does not do, and how the app tells you.
8. **[Validating the chain](validation.md)**: what the results are checked against, how to repeat it and what is missing.
9. **[Your files: disks, backup and archive](backup.md)**: move, free up space, back up and restore.

Running it on a server: [Deploy](deploy.md).

# Guide

[![The pipeline, step by step](images/pipeline.gif)](images/pipeline.mp4)

InSAR Orchestrator turns a polygon on a map into a ground-motion time series. This guide gets it running, explains the two ways to get the data and shows how to look after your files.

## 1. Install and start

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) and [Node.js](https://nodejs.org/) 18 or newer.

```bash
git clone https://github.com/JSempereH/InSAR-Orchestrator.git
cd InSAR-Orchestrator
./setup.sh   # once: installs everything and creates backend/.env
./dev.sh     # starts the app, Ctrl+C stops it
```

Open <http://localhost:5173>.

**MintPy** is the program that turns HyP3 results into a time series. It is optional and installs on its own. Without it everything works except the "Run MintPy" step:

```bash
uv pip install --python backend/.venv mintpy
```

## 2. Add your credentials

Open **Settings** and fill in only what you will use:

| For | Enter | Get it from |
|---|---|---|
| ASF / HyP3 | Your Earthdata username and password | A free account at [urs.earthdata.nasa.gov](https://urs.earthdata.nasa.gov/users/new) |
| EGMS | The full JSON key | A service-account key from your account at [land.copernicus.eu](https://land.copernicus.eu) |

Credentials are stored encrypted. The key that opens them is `SECRET_KEY` in `backend/.env`, which `./setup.sh` creates for you. Do not delete or change it. If it is lost, the app says *"Stored credentials could not be decrypted"* and you simply enter them again.

## 3. How it works: two ways to get ground motion

| | **ASF** | **EGMS** |
|---|---|---|
| What you get | Your own time series, computed from Sentinel-1 images | Ready-made products from Copernicus |
| Where | Anywhere Sentinel-1 flies | Europe |
| You choose | Your dates, the satellite track and the area | Area, product level and release |
| Needs | Earthdata account, HyP3 credits, MintPy | The EGMS key |
| Takes | HyP3 processing, then MintPy | Just the download |

Either way, the result opens in the **Viewer**.

### The ASF way

1. **ASF page, "+ New".** Name the project, set the dates, draw the area, pick a satellite track (the app shows how many scenes each one has) and confirm. *Max temporal neighbors* is how many following images each image is paired with (3 is a good start).
2. **Submit new batch.** HyP3 processes one interferogram per pair in the cloud. Each pair uses HyP3 credits, and your balance is at the top right. Statuses update on their own.
3. **Download.** Tick *Auto-download when done*, or download from the table. HyP3 keeps each result online only until its expiry date, and an expired result can no longer be downloaded (it would have to be processed again, using credits). Once a result is on your disk it is yours, whatever HyP3 does afterwards.
4. **Run MintPy.** The panel at the bottom of the project. It first **crops** the data to your area (see below), then builds the time series.
5. **Open in viewer.**

### The EGMS way

1. **EGMS page.** Draw the area (up to 5° x 5°), choose a *Level* and press **Search products**:
   - **L2a / L2b** give a displacement time series for every point.
   - **L3** gives velocity maps only (vertical and east-west), with no time series.
2. Tick the products you want and press **Download**.
3. Open them in the **Viewer**.

### What the crop does, and why

A HyP3 image covers hundreds of kilometres, but your area is a few. Before MintPy runs, the app keeps only a window around your area (with a 25% margin) and **deletes the original downloads**. A project goes from tens of GB to a few hundred MB.

The catch: the crop cannot be redone. If you later want a different area, create a new project and download again. That only works while HyP3 still hosts the results; if they have expired, the pairs have to be processed again.

### In the Viewer

Pick a dataset on the left, click a point and its time series opens in a window you can resize, move or open in a separate browser window. To understand what the numbers mean, read [Reading a time series](time-series-analysis.md).

## 4. Your data

| What | Where |
|---|---|
| Projects, batches, jobs | `backend/insar_app.db` |
| Settings and the encryption key | `backend/.env` |
| ASF projects | `backend/downloads/<project name>/` |
| EGMS downloads | `backend/downloads/egms/<name>/` |

**Choose a disk.** When you create a project or an EGMS download, the *Storage destination* menu lists the disks the computer can see. To change the default folder for everything, set `DOWNLOADS_DIR=/path/to/folder` in `backend/.env`.

**Move data to another disk.** On the **Downloads** page press **Move** next to a project or download, pick the destination and wait for the progress bar. The disk has to be mounted already (a network share works if it is mounted). Only one move runs at a time.

**Free up space.** On the **Downloads** page, *Delete downloaded interferograms* removes a project's files but keeps its history (you can download the results again only until they expire on HyP3), and *Remove* deletes an EGMS download from the list (it asks whether to delete the files too).

**Back up automatically.** Install [rclone](https://rclone.org/install/) on the computer that runs the app, then open **Settings, Backup**. The destination is either a folder on a mounted disk (`/mnt/backup/insar`) or `remote:path` for a remote you create once with `rclone config`: S3 and compatible services, or SFTP to another computer you can reach over SSH or a VPN. *Test destination* checks that it is writable, *Back up now* runs one immediately, and *Run automatically* repeats it on a schedule.

Tick or untick the projects you want included under *Projects to back up*. New projects are included by default, and an unticked one is skipped (its files only; the database snapshot still lists it). Each run copies every project folder and then a consistent snapshot of the database. Nothing at the destination is deleted, apart from old database snapshots beyond the daily and weekly retention you set. MintPy work files are skipped because they can be rebuilt. Raw SLC scenes are skipped too unless you tick *Also back up raw SLC scenes*: they cost no HyP3 credits (they come straight from ASF with your Earthdata login), only time and bandwidth, and they are several GB each. If any project folder fails to copy, the database snapshot is not uploaded, so a restored database never points at files the backup lacks. The same thing runs from cron or a systemd timer:

```bash
cd backend && .venv/bin/python -m app.backup --to s3backup:my-bucket/insar
```

`backend/.env` is **never** uploaded, because it holds the key to the stored credentials. Keep a copy of it somewhere separate from the backup.

**Archive a project.** When a project is finished and you need the disk space, press **Archive** next to it on the **Downloads** page and give a destination (it is prefilled with your backup destination). The app copies the whole project folder, MintPy results and SLC scenes included, verifies the copy with `rclone check`, checks that nothing in the folder changed meanwhile, and only then deletes the local files. If anything fails the local files stay exactly as they were. An archived project shows as *archived*: you cannot view it, update it or run MintPy until you press **Restore**, which copies it back and verifies it. The archive copy is never removed.

Verification compares sizes and, where the destination supports it, checksums. Some destinations have no checksum in common with a local disk (SFTP without `md5sum`, for example), and then only sizes are compared.

**Back up by hand.** Three things matter:

1. **The database.** Run `make backup-db`. It writes a safe copy to `backups/`, even while the app is running. Do not copy `insar_app.db` by hand: while the app runs, recent changes sit in a side file and a plain copy can miss them.
2. **`backend/.env`.** It holds the key to the stored credentials. Keep a copy somewhere separate from the database.
3. **Your project folders.** After the crop, the originals are gone, so a project folder is the only copy of that data. It is small, so copy it with your usual tool. EGMS downloads can be fetched again, so they are optional.

**Restore.** Stop the app, copy the backup over `backend/insar_app.db`, delete `insar_app.db-wal` and `insar_app.db-shm` if they exist, put `backend/.env` and the folders back in the same places, and start the app. On a new computer, run `./setup.sh` first: it never overwrites an existing `backend/.env`.

## If something goes wrong

- **"Stored credentials could not be decrypted"**: `SECRET_KEY` changed. Enter your credentials again in Settings.
- **"smallbaselineApp.py was not found"**: MintPy is not installed. See step 1.
- **The Viewer says "No MintPy results yet"**: run MintPy from the project's page in ASF first.
- **The map stays on "Loading map…"**: the map background comes from the internet. Check your connection and reload.

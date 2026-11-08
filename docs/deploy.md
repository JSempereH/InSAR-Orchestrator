# Deploying on a minipc or a VPS

The app ships as one container image with everything inside: the web app, the API, MintPy and rclone. It listens on one port, keeps all its data in one folder and never runs as root.

Pick a way to run it:

| | Needs root once | Best for |
|---|---|---|
| [Container with Docker or Podman](#container) | To install Docker or Podman, if it is not there yet | Most setups: nothing else to install, easy updates |
| [systemd user service](#without-containers) | Never | Machines where you cannot or do not want containers |

## Container

### Start it

```bash
git clone https://github.com/JSempereH/InSAR-Orchestrator.git
cd InSAR-Orchestrator
cp .env.example .env     # then edit it: set APP_PASSWORD
docker compose up -d --build   # or: podman compose up -d --build
```

Open <http://localhost:8000> and sign in. The first build takes a while: MintPy and GDAL are large. Later starts reuse it.

The first start creates `./data` with your database, your downloads, the key that encrypts stored credentials (`data/secret_key`) and your rclone remotes. **Copying that folder copies the whole app.**

### Who can reach it

By default the port is published on `127.0.0.1`, so only the machine itself can open it. Choose one:

- **Over a VPN (simplest).** Set `BIND` in `.env` to the machine's VPN address, for example `BIND=10.8.0.2`. Nobody outside the VPN can connect.
- **On the internet.** Keep `BIND=127.0.0.1` and put a reverse proxy with HTTPS in front. With [Caddy](https://caddyserver.com/), which gets the certificate by itself, the whole configuration is:

  ```
  insar.example.com {
      reverse_proxy 127.0.0.1:8000
  }
  ```

**Always set `APP_PASSWORD` when anyone but you can reach the port.** Without it, whoever connects can use your stored HyP3 credentials and spend your credits. With it, every request needs a signed-in session, five wrong passwords lock the login for a minute, and changing the password signs everybody out. The password protects the app; it is not a replacement for HTTPS on the open internet, because it travels in the login request.

### Without root

The container always runs as an ordinary user. What needs root is installing the tool that runs containers, and only if it is not installed yet.

- **Rootless Docker.** After a normal Docker install, run `dockerd-rootless-setuptool.sh install` as yourself.
- **Rootless Podman.** `sudo apt install podman` once, then everything runs as you. Put `USERNS_MODE=keep-id` in `.env` so files in `./data` belong to you.

`UID` and `GID` in `.env` are the user that owns `./data`. The defaults (1000) fit the first user on most systems; check with `id -u` and `id -g`.

### Disks for backups and archives

The container only sees what you mount into it. To back up or archive to a disk, mount it at the path you will type in the app, in `compose.yaml`:

```yaml
    volumes:
      - ./data:/data
      - /mnt/backup:/mnt/backup
```

For S3 or another computer, configure the remote once from inside the container; it is saved in `data/rclone`:

```bash
docker compose exec insar rclone config
```

Then use `remotename:path` as the destination in **Settings, Backup**. A **mounted disk must really be mounted** when the container starts, or the app writes to an empty folder on the system disk instead.

### Updates

```bash
git pull && docker compose up -d --build
```

The database upgrades itself on start. Take a backup first if the project holds data you cannot get again.

### Backups of the app itself

The in-app backup (Settings, Backup) copies projects and a consistent database snapshot. Also keep a copy of `data/secret_key` somewhere separate from those backups: it opens the stored credentials, and it is deliberately never uploaded.

## Without containers

`deploy/install-user.sh` installs the app as a systemd service of your own user. It needs [uv](https://docs.astral.sh/uv/getting-started/installation/) (installs into your home) and Node.js 18 or newer (only to build the web app once).

```bash
deploy/install-user.sh                  # listens on 127.0.0.1:8000
deploy/install-user.sh --host 0.0.0.0   # reachable from the network: set APP_PASSWORD in backend/.env first
loginctl enable-linger "$USER"          # keep it running after you log out and start it at boot
```

The script refuses to listen beyond localhost without `APP_PASSWORD`. Logs are in `journalctl --user -u insar-orchestrator -f`.

MintPy and rclone are optional and also install without root. The easiest way for MintPy, which needs GDAL, is [micromamba](https://mamba.readthedocs.io/en/latest/installation/micromamba-installation.html) (which is also a single file in your home):

```bash
micromamba create -y -n insar -c conda-forge python=3.11 mintpy gdal rclone
```

The service's `PATH` already includes `~/micromamba/envs/insar/bin`. Restart the service after installing.

## Atmospheric correction (weather data)

MintPy's ERA5 troposphere correction needs weather data. The app downloads it from the public ARCO-ERA5 archive, so the machine only needs internet access to `storage.googleapis.com`. No account is required. If the archive cannot be reached, a Copernicus Climate Data Store login in `~/.cdsapirc` is used instead (`data/home/.cdsapirc` with the container); without either, the app skips that correction and says so in the MintPy panel. Details in [Credentials](credentials.md#era5-weather-data-no-login-needed).

## Troubleshooting

- **"Permission denied" writing to `./data`**: `UID` and `GID` in `.env` do not match the owner of `./data`. Fix them, or `sudo chown -R` the folder once.
- **The page loads but every request says "Authentication required"**: sign in again; the session cookie is only sent over the address you signed in at, so do not mix `localhost` and the machine's name.
- **"smallbaselineApp.py was not found"** (without containers): MintPy is not on the service's `PATH`; see the micromamba step.
- **Stored credentials could not be decrypted**: `data/secret_key` (or `SECRET_KEY`) changed. Enter the credentials again in Settings.

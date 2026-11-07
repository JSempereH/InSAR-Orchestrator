# Credentials

Open **Settings** and fill in only what you will use.

![Settings, credentials](images/settings-credentials.png)

| For | Enter | Get it from |
|---|---|---|
| ASF / HyP3 | Earthdata username and password | A free account at [urs.earthdata.nasa.gov](https://urs.earthdata.nasa.gov/users/new) |
| EGMS | The full JSON key | A service-account key from your account at [land.copernicus.eu](https://land.copernicus.eu) |
| ERA5 correction | A `~/.cdsapirc` file (not entered in Settings) | A free account at [cds.climate.copernicus.eu](https://cds.climate.copernicus.eu) |

## How they are stored

Earthdata and EGMS credentials are stored **encrypted**. The key that opens them is `SECRET_KEY` in `backend/.env`, which `./setup.sh` creates. Do not delete or change it. If it is lost, the app says *"Stored credentials could not be decrypted"* and you enter them again.

## Copernicus Climate Data Store (optional, but recommended)

By default MintPy corrects the troposphere with ERA5 weather data, which it downloads from the Copernicus Climate Data Store. Create the file `~/.cdsapirc` on the machine that runs the app:

```
url: https://cds.climate.copernicus.eu/api
key: <your personal access token>
```

You may also have to accept the ERA5 licence once on the dataset's page. With the container, put the file at `data/home/.cdsapirc`.

**Without it** the app notices before the run, **leaves the ERA5 correction out, runs everything else and shows a warning** in the MintPy panel and in *Processing details*. (Letting MintPy try would stop late in the run with a division-by-zero error that does not mention credentials.) Add the file and press *Run again* to apply the correction. ERA5 files already downloaded into the project's `mintpy/ERA5` folder are reused.

<p align="center"><img src="packaging/icon.png" width="96" alt=""></p>

<h1 align="center">WebODM Height Fix NZ</h1>

<p align="center">
Puts your drone maps at the right height above sea level (NZVD2016), automatically.<br>
For WebODM / OpenDroneMap projects in <b>New Zealand</b>.
</p>

<p align="center"><img src="docs/screenshot.png" width="640" alt="WebODM Height Fix NZ window"></p>

---

## The problem

Ordinary (non-RTK) drones are good at measuring how high they are *above where they took off*, but bad at knowing how high that is above sea level. Their "absolute" altitude is often tens or even hundreds of metres out. So your WebODM elevation models, point clouds and 3D models come out at the wrong height.

WebODM Height Fix NZ lines your map up with **LINZ's national 1 m LiDAR** (which is in NZVD2016, New Zealand's official height system). It corrects the height, any tilt, and the few metres of horizontal GPS error, then exports everything in your local coordinate system. You don't need to know your takeoff height or have ground control points.

It also makes a **change map** showing your survey minus the LiDAR, so you can see ground that has moved since LINZ flew it, such as slips, slumps and earthworks.

## Download and install

1. Go to the [**Releases**](../../releases/latest) page and download `WebODM-Height-Fix-NZ-Setup-x.y.z.exe`.
2. Double-click it.
   - If Windows says *"Windows protected your PC"*, click **More info** and then **Run anyway**. This appears because the installer isn't code-signed yet.
3. Click **Next** through the installer. It doesn't need admin rights.

WebODM Height Fix NZ then appears in your Start menu. To remove it, go to *Settings > Apps > Installed apps*.

## How to use it

1. In WebODM, open your finished task and click **Download Assets > All Assets**. This gives you `all.zip`.
2. Open **WebODM Height Fix NZ**.
3. **Step 1:** click **Choose zip...** and pick that `all.zip`.
4. **Step 2:** the output folder is filled in for you, next to the zip. Change it if you like. Pick the **coordinate system** for the exports. The default is **NZGD2000 / Mount Eden 2000 (EPSG:2105)**. NZTM 2000 and all the other NZGD2000 meridional circuits are in the list.
5. **Step 3:** click **Check first** to see how far out the heights are. This changes nothing.
6. Click **Fix heights**. When it's finished, click **Open output folder**.

It needs an internet connection, because it reads the LINZ elevation data for your site. It usually takes a minute or two.

### How the lining up works

1. **Sideways:** it slides your map around to find where its shape best matches the LiDAR. This fixes the 1–3 m horizontal error typical of drones without RTK.
2. **Height and tilt:** it fits a gently sloping height correction to **stable open ground** only: roads, lawns and paddocks, where LINZ's surface and ground models agree. Trees and buildings are left out, and so is water, which LINZ flattens. Ground that has **changed since the LiDAR**, such as a slip, doesn't match. It's detected and left out of the fit, so it can't drag the correction with it.

The **Details** box shows what was found. Tested on a real site, open ground ended up within ±0.5 m of LINZ for 84% of the area, compared with 21% for a simple height shift.

> **Warnings to look out for:** *"a lot of the open ground differs from LINZ"* means a large part of the site has changed (a big slip or earthworks), so check the change map. *"The model is noticeably tilted"* means it was corrected, but ground control points would make the survey more reliable.

### What you get

| File | What it is |
|---|---|
| `<name> - orthophoto.tif` | the orthophoto as a GeoTIFF |
| `<name> - orthophoto.png` + `.pgw` + `.prj` | the same orthophoto as a **transparent PNG with a world file**, for CAD programs (BricsCAD, AutoCAD) that show GeoTIFF no-data as black |
| `<name> - DSM NZVD2016.tif` | the elevation model, in metres above sea level (NZVD2016). If your WebODM task made a DTM (ground only), you get `DTM` instead. If it made no elevation model at all, one is built from the point cloud. |
| `<name> - change since LINZ (m).tif` | your survey minus the LINZ LiDAR. **Negative means the ground is lower now**, for example a slip scarp. Positive means it's higher, for example slip debris or fill. Water is left blank. The Details box shows the area and volume lowered and raised by more than 0.5 m. |

All of them are in the coordinate system you picked. The orthophoto keeps all of the image, and the area outside the map is transparent. Elevation files use the no-data value `-9999`.

### For slips: use a DTM, not a DSM

For comparing ground surfaces, process your task in WebODM with **`dtm`** turned on (Task options > `dtm: true`). It also helps to turn on **`pc-classify`**. A DSM is the top surface, including trees, roofs and cars, so contours and TINs made from it look nothing like LINZ's bare-ground contours, and trees show up as "change". When the download includes `dtm.tif`, this tool lines it up against LINZ's bare-ground DEM, exports the DTM, and builds the change map from it.

Tick **Also export the point cloud and 3D model** to get corrected copies of these files too:

| File | Corrected? |
|---|---|
| `odm_georeferencing/odm_georeferenced_model.laz` (point cloud) | ✅ |
| `odm_texturing/odm_textured_model_geo.obj` (3D model, with its textures) | ✅ |
| `odm_dem/dsm.tif`, `odm_dem/dtm.tif` (WebODM's own elevation models, uncropped) | ✅ |
| `.glb` 3D model, web viewer tiles | ❌ Not corrected. Use the `.obj` instead. |

**Your original files are never changed.** These extra files are moved, levelled and tilted the same way, but they stay in WebODM's UTM coordinate system.

## Advanced options

Tick **Show advanced options** in the window for these other methods:

- **GNSS ellipsoid → NZVD2016:** only for RTK/PPK drones whose heights are true GNSS ellipsoidal heights. Subtracts the NZGeoid2016 geoid height.
- **Raw DJI photos: takeoff height + relative altitude:** corrects the photos *before* WebODM. You enter the real ground height where you took off.
- **Fixed offset:** adds or subtracts a number of metres that you choose.

There's also a command-line version: run `WebODMHeightFixNZ.exe --help` from the install folder.

## FAQ

**My site isn't covered by LINZ LiDAR.** Almost all of New Zealand is covered by the 1 m LiDAR elevation model. Where it isn't, the tool falls back to LINZ's 8 m national elevation model.

**I used ground control points (GCPs) surveyed in NZVD2016.** Then your heights are already right. Don't run this tool on them.

**I need Auckland Vertical Datum 1946 (AVD-46), or another local datum.** That isn't supported. The output is always NZVD2016.

**Does it change horizontal positions?** Yes, in Automatic mode. Everything is moved by the shift that best lines up with the LiDAR, usually 1–3 m. The other modes only change heights.

**How accurate is the change map?** On stable ground it's typically within ±0.3–0.5 m. Treat differences smaller than about 0.5 m with caution. The LiDAR may be a few years old, and trees, buildings and water always show up as differences in a DSM.

## Data and credits

- Elevation data: [LINZ](https://www.linz.govt.nz/) NZ elevation models, sourced from the LINZ Data Service and licensed for reuse under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- NZGeoid2016 grid: LINZ, CC BY 4.0, via the [PROJ data CDN](https://cdn.proj.org/).
- Built with [rasterio](https://rasterio.readthedocs.io/)/GDAL, [pyproj](https://pyproj4.github.io/pyproj/)/PROJ and [laspy](https://laspy.readthedocs.io/).

This is not an official LINZ product. Check your results before relying on them for anything important.

---

## For developers

**Run from source:** double-click `run.bat`. The first time, it downloads [uv](https://github.com/astral-sh/uv) and a private Python 3.13 into `.tools\`. It never uses or changes any Python already on the PC.

**Build the app and installer locally:**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

This writes the app to `dist\WebODMHeightFixNZ\` and, if [Inno Setup 6](https://jrsoftware.org/isinfo.php) is installed, `dist\WebODM-Height-Fix-NZ-Setup-<version>.exe`.

**Release a new version:**
1. Bump `__version__` in `nz_height_fix.py` and commit.
2. Push a matching tag, e.g. `git tag v1.0.1` then `git push origin v1.0.1`.
3. GitHub Actions builds the installer and publishes it on the Releases page.

Every push to `main` also builds the installer. You can download it from the workflow run's *Artifacts*.

| Path | What it is |
|---|---|
| `nz_height_fix.py` | the whole program: GUI, command line and the correction code |
| `packaging/nz_height_fix.spec` | PyInstaller build definition |
| `packaging/installer.iss` | Inno Setup installer script |
| `packaging/make_icon.py` | draws `icon.ico` / `icon.png` |
| `scripts/build.ps1` | build script, used locally and in CI |
| `.github/workflows/build.yml` | CI build and release |

Licence: [MIT](LICENSE).

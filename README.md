# Smart File Renamer

A local Python desktop app for batch renaming files from TXT or CSV names and
converting images in batches. No API key is needed. Background removal downloads
its model on first use; subsequent runs can use the cached model offline.

## Appearance

Use the **Theme** selector at the top right to switch between **Dark** and
**Light**. The app remembers your choice on restart. Theme changes keep the
current batch preview, image colors, and conversion settings unchanged.

## Workflow

1. In **Rename files**, click **Load names** and select a TXT file with one new filename per line.
2. Click **Select folder** and select the folder containing your files.
3. Choose **All files**, **Images**, **Videos**, or **Audio**. Optionally include subfolders.
4. Select the file order and click **Preview**. Each TXT line matches the file in that row.
5. Check the current and new filenames, then click **Rename files**.

The TXT file must have exactly as many nonempty lines as selected files. Blank
lines are ignored. UTF-8 (including BOM) and UTF-16 with BOM are supported.
Names can contain spaces and Unicode characters. The loaded TXT file is excluded
from the files being renamed, even when it is inside the selected folder.

Example `names.txt`:

```text
Beach sunrise
Family picnic
Weekend trip
```

With files `IMG_1.jpg`, `IMG_2.png`, and `VID_3.mp4`, the default preview is:

| Current file | New file |
| --- | --- |
| IMG_1.jpg | Beach sunrise.jpg |
| IMG_2.png | Family picnic.png |
| VID_3.mp4 | Weekend trip.mp4 |

**Keep extensions** is enabled by default. Names may omit extensions or include
the matching extension. Uncheck it to use TXT lines as complete filenames.
Changing an extension does not convert the file's contents.

The default order is **Modified date, oldest first**. Choose **Modified** or
**Created**, then **Oldest first** or **Newest first**. The preview shows the
selected timestamp, including fractional seconds, beside each source and new
name. Equal timestamps use natural filename order for a consistent result.
Natural ordering places `file2` before `file10`; alphabetical order is also
available. Creation time must be available on the filesystem; it is never
silently replaced by modification time. Files in subfolders remain in their original
folders. All files are supported without decoding their contents; media filters
recognize common file extensions. Symbolic links are skipped.

## Exact Filename Mapping

To avoid assigning a name to the wrong file, use a CSV mapping instead of an
ordered TXT list:

1. Select a folder and click **Preview**; a names file is optional for this step.
2. Click **Export mapping** to create a CSV of the displayed files.
3. Edit the CSV's `new_name` column. Keep `current_file` and `identity` unchanged.
4. Load that CSV with **Load names**, preview, and rename.

Each CSV row matches a source by its exact relative filename, including its
extension and subfolder. Changing the display order cannot change those pairs.
Exported CSVs also contain a file identity snapshot; a replaced or edited source
blocks the batch. Missing or extra files and duplicate source rows are blocked.
The selected names file is excluded from renaming.

A manually created CSV may use just these two columns:

```csv
current_file,new_name
IMG_1.jpg,Beach sunrise
IMG_2.png,Family picnic
VID_3.mp4,Weekend trip
```

TXT lists still match by the displayed order. Changing the order, date type,
filter, or names file clears the old preview. Once previewed, assignments stay
attached to those exact source paths until the operation finishes. Double-click
a source row to open the file for inspection.

## Batch Image Conversion

1. Open **Image tools** and select the source folder.
2. Choose an output folder; the default is a `converted` subfolder.
3. Choose **Convert images**, an input filter, and output format: **JPG**, **PNG**, **WebP**, **BMP**,
   **TIFF**, or **AVIF**.
4. Adjust quality for JPG/WebP/AVIF, the JPG/BMP background color, and SVG raster width.
5. Click **Preview**, check the thumbnails and output filenames, then **Convert images**.

Common inputs include WebP, JPG, PNG, GIF, BMP, TIFF, AVIF, HEIC/HEIF, and SVG.
Raster decoding uses Pillow and pillow-heif; SVG rasterization uses Qt SVG.
Unsupported or corrupt inputs are shown as preview errors. RAW camera files
require a separate decoder and are not converted by this version. Output images
use standard 8-bit color channels. Animated and multipage files require explicitly
enabling **First frame**, which produces a still image.
Images exceeding 40 million pixels are reported as preview errors.

Original files stay intact. Transparent pixels are preserved in compatible
outputs and composited onto the selected background for JPG/BMP. EXIF orientation
is applied to the pixels, and EXIF/ICC metadata is retained where supported.
Output modification times match the originals so modification-date ordering
remains useful; output creation times reflect the new files. Subfolder structure
is preserved, and the output folder is excluded from recursive source scans.

Duplicate output names and existing outputs block the preview. Outputs are also
checked during conversion and are never overwritten. **Cancel** stops before
the next image; completed outputs remain available. Any errors during execution
appear in their rows. Conversion is separate from rename undo because originals
are kept.

## Bulk Background Removal

1. Open **Image tools** and select the source folder.
2. Choose **Remove background** and an output folder.
3. Choose the input filter and optionally enable subfolders or **First frame**.
4. Click **Preview**, check the transparent PNG output filenames, then
   **Remove backgrounds**.

Background removal writes PNG files with transparent backgrounds and keeps the
original images unchanged. The output folder is checked the same way as
conversion: duplicate output names and existing files block the preview, and
outputs are never overwritten.

Background removal requires `rembg` with its CPU runtime. Install it into the
same virtual environment used by `Start.cmd`, then restart the app:

```powershell
.venv\Scripts\python -m pip install "rembg[cpu]>=2.0"
```

The first removal downloads the model automatically and can take longer.

## Background Colors And Gradients

1. Open **Image tools**, select the source and output folders, and choose
   **Add background**.
2. Select **Solid**, **Linear gradient**, or **Radial gradient**. Choose a built-in
   preset or click the color swatches to choose custom colors.
3. For linear gradients, choose **Horizontal**, **Vertical**, or **Diagonal**.
   The swap button reverses the two colors. Radial gradients run from the first
   color in the center to the second at the edges.
4. Enable **Remove existing background** for images that still have their original
   backgrounds. Leave it unchecked for transparent cutouts from a previous removal.
5. Choose the output format, click **Preview**, then **Add backgrounds**.

Each table row shows a preview of its assigned background, canvas shape, and
image fit beside the source filename. The small preview shows the selected row.
Existing backgrounds are removed during the batch, rather than during preview.
Solid and gradient backgrounds fill transparent pixels and preserve the subject,
including partially transparent edges. Finished backgrounds are opaque and can
be saved in any supported output format.

Use the save icon beside **Preset** to name and store a custom color or gradient.
Saved presets remain available after restarting the app. Select a saved preset
and use the trash icon to delete it. Originals and existing output files remain
protected.

### Custom Canvas Size

In **Add background**, choose a **Canvas** preset or **Custom**, then enter width
and height in pixels. **Original size** keeps each image's dimensions. Available
presets include square, portrait, story, and landscape sizes.

**Fit inside** centers and scales each image proportionally to fit the canvas;
remaining space uses the selected background. **Fill and crop** scales to cover
the canvas and crops the excess from the center. The preview reflects the canvas
shape. Canvas dimensions are limited to 20000 pixels per side and 40 million
pixels total.

### Random Backgrounds In Bulk

Select the source folder containing your bulk images, choose **Add background**,
and set **Style** to **Random solid**, **Random linear gradient**, **Random radial
gradient**, **Random gradient**, or **Random mixed**. Random gradients use your
first swatch as the shared start color and give each image its own random end
color. For example, **Random radial gradient** produces white-to-red,
white-to-blue, and white-to-yellow backgrounds with the same radial shape.
Changing the first swatch to red replaces the white center in every row while
keeping each row's random outer color. Existing row previews update immediately.
Random colors are selected across the batch to avoid duplicates and spread colors
apart, so records look more distinct. Very large batches may still have similar
shades, but each generated color remains unique within the batch.
The second swatch displays the selected row's random color.

**Random linear gradient** keeps the chosen direction. **Random radial gradient**
keeps the centered radial shape. **Random gradient** chooses between linear and
radial, and **Random mixed** also includes random solid backgrounds. Custom
canvas sizes work with all these modes, and shared colors can be saved as presets.

Click **Preview** and select rows to inspect their assigned backgrounds. The
gradient settings are fixed for that preview and export, so rerunning Preview
keeps the assignments. Use the random refresh button beside the swatches to
generate new random colors while keeping your shared start color, then preview
again. Enable **Remove existing background** for opaque photos
when you want their original background replaced.

## Safety And Undo

The preview blocks mismatched counts, duplicate destinations, invalid Windows
filenames, and collisions with existing files or folders. Files are checked again
before renaming. Existing files are never overwritten. Swaps and changes only
to letter casing are supported.

**Undo last batch** restores the last successful batch, including after restarting
the app. Undo is blocked if a file has changed or an original name is occupied.
A failed batch attempts to restore the original names. If the application is
interrupted during a batch, **Recover batch** restores the names before more files
can be renamed. Rename history and logs are stored in the user's application
data directory, outside the selected folder. Each successful batch replaces the
previous undo history.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python main.py
```

On Windows, `Start.cmd` also launches the app using `.venv`.

Load a names file and folder on startup (preview only):

```powershell
.venv\Scripts\python main.py --names "C:\Files\names.txt" --folder "C:\Files\Photos"
```

## Tests

```powershell
.venv\Scripts\python -m unittest discover -s tests
```

## Packaging

```powershell
.venv\Scripts\python -m pip install pyinstaller
.venv\Scripts\python -m PyInstaller --noconfirm --windowed --name SmartFileRenamer main.py
```
#   s m a r t _ i m a g e _ t o o l s  
 

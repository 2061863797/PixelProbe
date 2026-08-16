# PixelProbe

[简体中文](https://github.com/2061863797/PixelProbe/blob/main/README.md) | English

**Analyze images and videos with frame-, time-, coordinate-, and pixel-level precision.**

PixelProbe is a local command-line tool for image and video analysis. It can locate key changes in a video, extract exact frames, read pixel colors, calculate regional statistics, and turn long videos into timelines, spatiotemporal slices, and statistical images. Media files are always processed locally.

## Features

- **Media information**: Inspect dimensions, frame rate, frame count, duration, codec, and variable-frame-rate metadata.
- **Exact frame extraction**: Extract a frame by presentation index or timestamp, with optional cropping and preview scaling.
- **Pixel inspection**: Read RGB, HEX, HSV, Lab, and luminance values for one or more pixels.
- **Region analysis**: Calculate mean, median, extrema, standard deviation, and color statistics for a selected region.
- **Change detection**: Scan a point, region, or full frame to locate the strongest changes and their timestamps.
- **Color timelines**: Track how fixed pixels or grid regions change throughout a video.
- **X–T / Y–T slices**: Visualize motion, flicker, and shot changes in a single image.
- **Temporal reduction**: Reveal patterns hidden in noise, slowly changing watermarks, and motion-energy distributions.
- **One-command scan**: Produce a representative-frame sheet, change events, and black-, white-, or flash-frame findings.
- **Frame comparison and contact sheets**: Locate differences between two frames or browse a full video as a sampled grid.
- **Optical-flow and frequency analysis**: Analyze motion direction, periodic flicker, stripes, and moiré patterns.
- **Unified representation generation**: Generate X–T, Path–T, ROI–T, reduction, or optical-flow results from one request.
- **Verifiable result bundles**: Store exact numeric data, previews, coordinate mappings, provenance, and SHA-256 integrity records.

## Installation

For most users, download the standalone CLI from [GitHub Releases](https://github.com/2061863797/PixelProbe/releases/latest). Python is not required.

| System | Download |
| --- | --- |
| Windows x86-64 | `pixelprobe-windows-x86_64.zip` |
| Linux x86-64 | `pixelprobe-linux-x86_64.tar.gz` |
| macOS Apple silicon | `pixelprobe-macos-arm64.tar.gz` |

Extract the archive, then run:

```bash
./pixelprobe --version       # Linux / macOS
.\pixelprobe.exe --version  # Windows PowerShell
```

For optional optical-flow, Zarr, or MCP support, install a pinned release with Python 3.11 or later:

```bash
python -m pip install "pixelprobe @ git+https://github.com/2061863797/PixelProbe.git@v1.0.0"
```

See the [installation guide](https://github.com/2061863797/PixelProbe/blob/main/docs/installation.md) for PATH setup, checksums, optional dependencies, updates, and uninstall instructions.

## Quick Examples

```bash
# Show media information
pixelprobe info input.mp4

# Extract frame 120
pixelprobe frame input.mp4 --frame 120 --output frame120.png

# Extract and crop the frame at 3.5 seconds
pixelprobe frame input.mp4 --time 3.5 --crop 400,200,300,300 --output crop.png

# Inspect two pixels
pixelprobe pixel input.mp4 --frame 120 --point 520,340 --point 600,400

# Read native image samples, including alpha, palette indexes, or 16-bit values
pixelprobe pixel input.png --sample native --point 520,340 --json

# Analyze a rectangular region
pixelprobe region input.mp4 --frame 120 --rect 400,200,200,150

# Export a pixel-color timeline
pixelprobe timeline input.mp4 --point 520,340 --output timeline.png --csv timeline.csv

# Find the 10 strongest changes in a region
pixelprobe changes input.mp4 --rect 400,200,200,150 --top 10

# Generate a representative-frame sheet, change events, and anomalous-frame findings
pixelprobe scan input.mp4 --sheet-output overview.png

# Create a temporal standard-deviation image
pixelprobe reduce input.mp4 --op std --output temporal-std.png

# Compare two frames and locate the changed region
pixelprobe compare input.mp4 --frame-a 100 --frame-b 101 --output diff.png

# Sample 9 evenly spaced frames into a contact sheet
pixelprobe sheet input.mp4 --count 9 --output sheet.png

# Detect periodic flicker
pixelprobe spectrum input.mp4 --source luma

# Calculate dense optical flow
pixelprobe flow input.mp4 --frame-a 100 --frame-b 101 --flow-output flow.png
```

## Example Result

The short video below contains low-contrast digits obscured by strong noise. A single frame is difficult to interpret reliably, but aggregating consecutive frame intervals makes the time-varying digit structure much clearer:

- [View or download the input video](https://github.com/2061863797/PixelProbe/blob/main/docs/assets/pixelprobe-noise-demo.mp4)
- The result is divided into six consecutive intervals with traceable frame indexes and time ranges.

![Noise-video analysis across consecutive frame intervals](https://raw.githubusercontent.com/2061863797/PixelProbe/main/docs/assets/pixelprobe-noise-analysis.png)

This example illustrates PixelProbe's role: the agent uses vision to understand the image, while PixelProbe supplies exact frame indexes, time ranges, and deterministic numeric processing as evidence. The numeric result does not replace semantic judgment.

By default, `pixel` returns the legacy-compatible display RGB8 value. `--sample native` is available only for images. Its response explicitly reports `sample_semantics`: recognized common lossless formats use `stored_sample`, while JPEG and other lossy or uncertain formats use the selected decoder's `decoded_sample`. PixelProbe never describes a decoded JPEG value as the original RGB value before compression.

## Generating a Reproducible Result Bundle

`generate` accepts a JSON request and writes exact numeric data, coordinate mappings, previews, and reproducibility information to a `.bundle` directory. The following request generates an X–T representation:

```json
{
  "source": {"source_id": "source_main", "kind": "file", "uri": "input.mp4"},
  "selection": {"mode": "all", "sample_every": 1},
  "representation": "xt",
  "geometry": {
    "type": "line",
    "coordinate_space_id": "storage_pixels",
    "points": [[0, 120], [1919, 120]],
    "closed": false
  },
  "feature": {"name": "rgb", "config": {}},
  "output": {"format": "bundle", "include_preview": true}
}
```

Save it as `request.json`, then run:

```bash
pixelprobe generate input.mp4 --request request.json --output result.bundle
pixelprobe validate result.bundle
```

A JSON array can contain multiple requests. Requests for the same media file share one decode pass. By default, `validate` verifies the size and SHA-256 digest of every registered file. `--metadata-only` checks only structure and paths, so it cannot establish content integrity.

`feature_t` can generate exact per-frame grayscale, HSV, Lab, absolute frame-difference, temporal FFT, spatial FFT, STFT, and Farneback optical-flow data. STFT requests must explicitly provide `window`, `length`, `hop`, `padding`, and `normalization`. An irregular timeline produces an explicit error instead of being silently treated as uniformly sampled. Temporal FFT also rejects VFR input by default; only an explicit `"vfr_policy": "estimate"` enables compatibility mode with an estimation flag. Color-conversion and frequency-domain images are previews derived from Data.

Long-running jobs can use both the content cache and checkpoints. A checkpoint can resume only when the plan, input file, operator versions, and request all match exactly:

```bash
pixelprobe generate input.mp4 --request request.json --output result.bundle \
  --cache-dir .pixelprobe-cache --checkpoint result.checkpoint.json
pixelprobe generate input.mp4 --request request.json --output result.bundle \
  --cache-dir .pixelprobe-cache --resume-from result.checkpoint.json
```

`pixelprobe validate result.bundle --strict` also treats unknown optional fields and unregistered files as errors.

## Connecting an AI Agent through MCP

PixelProbe MCP is a thin adapter over the existing Python core; it neither copies nor changes the analysis algorithms. It communicates locally over `stdio`. The agent interprets people, objects, text, scenes, and composition, while PixelProbe verifies frame numbers, timestamps, coordinates, pixels, regional statistics, change candidates, and exact numeric representations.

Add the local server to an MCP-compatible agent:

```json
{
  "mcpServers": {
    "pixelprobe": {
      "command": "pixelprobe-mcp",
      "env": {
        "PIXELPROBE_MCP_ROOTS": "D:\\media"
      }
    }
  }
}
```

The client starts `pixelprobe-mcp` automatically; do not run it manually in a regular terminal. Separate multiple allowed directories in `PIXELPROBE_MCP_ROOTS` with a semicolon on Windows or a colon on macOS and Linux. The server rejects paths outside those roots. Formal Bundles are written by default to `.pixelprobe-mcp/artifacts/` under the first allowed root. `PIXELPROBE_MCP_ARTIFACT_ROOT` can select another directory within an allowed root.

After connecting, the agent should call `pixelprobe_inspect_media` first, then use `pixelprobe_get_frame` to inspect the original frame with its own vision, and finally call precise pixel, region, change, or Artifact tools as needed. MCP also exposes the `pixelprobe_analyze_media` Prompt and the `pixelprobe://guidance` Resource, which reinforce the principle that visual understanding is primary and deterministic data is supporting evidence.

For images, the response separately describes stored sample channels, the RGB channels used for deterministic analysis, and the visual PNG channels. It also reports palette usage, complete alpha statistics, and candidates for regular high-frequency textures. A texture candidate includes the analyzed coverage, period, and correlation evidence. It is only a prompt for agent review and is never presented as confirmed compression, corruption, or manipulation.

`pixelprobe_get_frame` does not resize the image. If the original PNG exceeds the client's payload limit, the call fails explicitly and recommends cropping or tiled inspection; it never silently returns a thumbnail. Representation generation is the only write operation. It can write only to the controlled Artifact directory and never overwrites an existing result. All other tools are read-only.

### Updating MCP

After updating PixelProbe, restart the MCP server process so the client loads the new version. For a cloned repository, run:

```bash
git pull
python -m pip install -e ".[mcp]"
```

For a direct GitHub installation, replace `<release-tag-or-commit-hash>` with the target revision and force reinstallation:

```bash
python -m pip install --upgrade --force-reinstall "pixelprobe[mcp] @ git+https://github.com/2061863797/PixelProbe.git@<release-tag-or-commit-hash>"
```

Then restart the MCP-compatible agent or desktop client and call `pixelprobe_get_capabilities` to confirm `pixelprobe_version`.

## Command Reference

| Command | Purpose |
| --- | --- |
| `info` | Show image or video information |
| `frame` | Extract a frame by presentation index or timestamp |
| `pixel` | Inspect one or more pixels; `--sample native` reads native image sample values |
| `region` | Analyze a rectangular region |
| `timeline` | Generate a pixel-color timeline |
| `xt` | Generate an X–T slice from a horizontal scan line |
| `yt` | Generate a Y–T slice from a vertical scan line |
| `changes` | Locate the strongest changes and group them into events |
| `scan` | Generate a contact sheet, change events, and anomalous-frame findings |
| `reduce` | Generate per-pixel mean/median/min/max/std/diff images |
| `compare` | Compare two frames and generate a difference image |
| `sheet` | Sample evenly spaced frames into a contact sheet |
| `spectrum` | Run a temporal FFT to detect periodic changes |
| `spectrum2d` | Run a spatial FFT on one frame to detect stripes or moiré patterns |
| `flow` | Calculate dense optical flow and estimate global motion |
| `generate` | Generate one or more exact representations and a Bundle from a request |
| `validate` | Fully validate Bundle structure, files, and SHA-256 digests |
| `cache clear` | Remove safe-to-delete local execution cache entries |

Run `pixelprobe <command> --help` to see all options for a command.

## Output Modes

All commands support `--json`. Analysis commands generally also support:

- `--quiet`: Show only essential output.
- `--verbose`: Show detailed error information.
- `--no-progress`: Disable progress bars.

For scripts and automation, use JSON mode and check the process exit status:

```bash
pixelprobe info input.mp4 --json
```

Business errors map to stable nonzero exit codes. Diagnostics are written to standard error and never contaminate JSON output.

## Coordinate and Time Conventions

- The origin is at the top-left; x increases to the right and y increases downward.
- Coordinates always refer to the original media resolution.
- Presentation frame indexes start at `0`.
- Frame ranges in common analysis commands include both the start and end frames.
- In a `generate` request, `requested_end_frame_exclusive` and temporal end values are excluded from the interval.
- Public time starts at `0` seconds on the first media frame.
- A time lookup selects the last frame whose timestamp is not later than the requested time.
- Variable-frame-rate video uses decoded frame timestamps rather than an average-frame-rate estimate.

A change peak means only that the pixel difference is large. It does not prove that an object moved or that a semantic event occurred. Inspect original frames before, at, and after a candidate event before drawing a conclusion.

## Inputs and Outputs

Images support common formats such as PNG, JPEG, BMP, and WebP. Videos support common containers such as MP4, MKV, AVI, and MOV. Exact codec availability depends on the local PyAV/FFmpeg build.

Results can be written as:

- PNG: Extracted frames, crops, timelines, spatiotemporal slices, and analysis images.
- CSV: Pixel timelines and per-frame change records.
- JSON: Structured results for scripts and automation.
- NPY: Exact dtype, shape, and numeric values with partial-read support.
- Zarr v3: Optional storage for large chunked arrays.
- Bundle: Data, Preview, coordinate mappings, source identity, ExecutionPlan, execution events, provenance, and integrity records.

Preview is display-only and never replaces or resizes Data. Clearing the local cache never deletes a Bundle.

## License

PixelProbe is licensed under the [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0). Use, modification, and distribution are subject to the full terms in this repository's `LICENSE` file. Third-party dependencies remain subject to their respective licenses.

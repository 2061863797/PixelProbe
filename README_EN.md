# PixelProbe

[简体中文](https://github.com/2061863797/PixelProbe/blob/main/README.md) | English

**Analyze images and videos with frame-, time-, coordinate-, and pixel-level precision.**

PixelProbe fills the gaps in how AI analyzes images and videos: the AI understands what the picture shows, while PixelProbe measures, locates, and gathers forensic clues, returning verifiable frame numbers, timestamps, coordinates, pixel values, change intervals, and stream-structure evidence. It is also a complete local command-line tool, and it is available to AI assistants through MCP and Claude Code / Codex plugins. Media files are always processed locally.

> **Upgrading from 1.x?** 2.0 uses a single `--at` option for time and location, and MCP is reduced to 10 tools; old options and tool names were removed. See the mapping tables in [docs/migration-2.0.md](docs/migration-2.0.md) (Chinese).

## Where It Helps

When an AI looks at images and videos, it is good at saying *what* is in the picture but unreliable at saying *which frame, which millisecond, which coordinate, what value*. It also easily misses a flash that lasts one or two frames, or a faint signal that only appears across many consecutive frames. PixelProbe fills these gaps with deterministic computation, and every number traces back to a frame index, timestamp, coordinate, and parameter set.

| Scenario | Questions it answers | Typical commands / tools |
|---|---|---|
| **UI and screen-recording testing** | Which frame does the button appear on? How many milliseconds does the animation take? Are frames dropped or frozen? Where do two screenshots differ? Is a control's color exactly right? | `find`, `changes`, `compare`, `pixel`, `track` |
| **Helping an AI watch long videos** | Which part of a two-hour video is worth looking at? Exactly which frame and where does the change happen? | `index build`, `scan`, `find`, `sheet` |
| **Video quality checks** | Black, white, or flash frames? Freezes, periodic flicker, moiré, block artifacts, bitrate drops? | `find`, `scan`, `spectrum`, `spectrum2d`, `streams` |
| **Image and video forensics** | What EXIF is present (camera, capture time, lens)? Signs of recompression, inconsistent noise, C2PA credentials? Anomalous keyframes or timestamps? | `evidence`, `streams` |
| **Hidden or faint signals** | Patterns that look like noise in one frame, slowly changing watermarks, motion-energy distribution | `reduce`, `xt` / `yt`, `spectrum` |

**Practical value:**

- **From "roughly" to "exactly"**: instead of "the button appears at about one second", PixelProbe reports "frame 30, 1.000 s, pixel (40,90,220)", which can go straight into a test assertion or bug report.
- **Short problems are not missed**: a one-frame flash or a half-second stall only shows up in a frame-by-frame check; an AI sampling frames easily skips it.
- **Saves the AI's context**: index and scan a long video first, then look only at the moments that matter instead of feeding the whole video to the model.
- **Results can be re-checked**: formal results are saved as bundles with SHA-256 integrity records, parameters, coordinates, and provenance, so others can recompute and compare.
- **Everything runs locally**: media is never uploaded, and MCP reads only directories you allow.

**What it does not do**: it does not recognize image content, run OCR, edit or transcode media, or decide whether media is authentic. Those are left to the AI or dedicated tools; PixelProbe supplies reviewable evidence and numbers. See the [positioning document](docs/positioning.md) (Chinese).

## New in 2.0

- **Unified addresses**: `--at "t=3s..5s rect(400,200,200,150)"`, `cell(C4)`, `@event1`, identical in the CLI and MCP ([docs/address.md](docs/address.md)).
- **Query search**: `find "black | flash | freeze >= 0.5s | gap"`, and `"animation in rect(...)"` to time UI animations ([docs/find.md](docs/find.md)).
- **Media index**: decode a long video once into a per-frame table, then answer whole-frame questions by lookup ([docs/media-index.md](docs/media-index.md)).
- **New slices**: difference slices, adaptive time axes, point tracking with follow slices ([docs/slices.md](docs/slices.md)).
- **Stream structure**: container tags, keyframe intervals, timestamp gaps, and bytes per second as forensic and quality clues ([docs/streams.md](docs/streams.md)).
- **AI-oriented output**: images carry coordinate rulers and grids, results carry reusable names, and the plugin ships four scenario Skills ([docs/plugins.md](docs/plugins.md)).

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

Official releases contain the Python wheel and source archive only. They do not publish a standalone executable containing third-party native libraries such as FFmpeg. Install a pinned release with Python 3.11 or later:

```bash
python -m pip install "pixelprobe[mcp,flow,storage,evidence] @ git+https://github.com/2061863797/PixelProbe.git@v2.0.0"
pixelprobe --version
```

MCP users should prefer the pinned [uvx configuration](docs/uvx.md). Developers may create a private local executable from their own environment:

```bash
python -m pip install -e ".[dev,mcp,flow,storage,evidence]" pyinstaller
python scripts/build_local_standalone.py --output build/local-pixelprobe
```

The script runs real CLI/MCP checks against the local frozen executable by default and writes `LOCAL-BUILD.txt` plus `build-info.json`. The result is not an official release and must not be redistributed before reviewing the licenses of the native libraries selected by the local environment.

See the [installation guide](https://github.com/2061863797/PixelProbe/blob/main/docs/installation.md) for uvx, wheel installation, optional dependencies, updates, local builds, and uninstall instructions.

## Quick Examples

Time and position are given with a single `--at` option, for example
`"f=120 rect(400,200,200,150)"`, `"t=1s..3s step=2"`, or `"cell(C4)"`.
See the [address syntax](docs/address.md) for the full grammar.

```bash
# Show media information
pixelprobe info input.mp4

# Extract frame 120
pixelprobe frame input.mp4 --at "f=120" --output frame120.png

# Extract and crop the frame at 3.5 seconds
pixelprobe frame input.mp4 --at "t=3.5s rect(400,200,300,300)" --output crop.png

# Inspect two pixels
pixelprobe pixel input.mp4 --at "f=120 point(520,340) point(600,400)"

# Read native image samples, including alpha, palette indexes, or 16-bit values
pixelprobe pixel input.png --sample native --at "point(520,340)" --json

# Analyze a rectangular region, or one cell of an 8-column x 6-row grid
pixelprobe region input.mp4 --at "f=120 rect(400,200,200,150)"
pixelprobe region input.mp4 --at "f=120 cell(C4)"

# Export a pixel-color timeline
pixelprobe timeline input.mp4 --at "point(520,340)" --output timeline.png --csv timeline.csv

# Find the 10 strongest changes in a region between 10 s and 20 s
pixelprobe changes input.mp4 --at "t=10s..20s rect(400,200,200,150)" --top 10

# Generate a representative-frame sheet, change events, and anomalous-frame findings
pixelprobe scan input.mp4 --sheet-output overview.png

# Index a long video once; full-frame change detection then reads the index with identical results
pixelprobe index build input.mp4

# Create a temporal standard-deviation image
pixelprobe reduce input.mp4 --op std --output temporal-std.png

# Compare two frames and locate the changed region
pixelprobe compare input.mp4 --at "f=100,101" --output diff.png

# Sample 9 evenly spaced frames into a contact sheet
pixelprobe sheet input.mp4 --count 9 --output sheet.png

# Detect periodic flicker
pixelprobe spectrum input.mp4 --source luma

# Calculate dense optical flow
pixelprobe flow input.mp4 --at "f=100,101" --flow-output flow.png
```

## Worked Example: A Simulated Screen Recording

The repository includes a script that generates a simulated screen recording with fully defined pixel values (640×360, 30 fps, 4 seconds, lossless): a blue button appears on frame 30, the whole frame flashes white for one frame at frame 60, and a moving block stops on frames 80–109. The outputs below were produced by actually running the commands on this video, so you can reproduce each one:

```bash
python examples/make_demo_video.py demo.mkv
```

The CLI prints Chinese table labels; they are translated below, and the values are unchanged.

**1. Find flash frames and freezes**

```bash
pixelprobe find demo.mkv "flash | freeze >= 0.5s"
```

```text
Search results: 2 intervals, 120 frames analyzed (decoded)
Name     Condition  Start  End  Start s  End s  Value
@match1  flash      60     60   2.000    2.000  0.23046
@match2  freeze     79     109  2.633    3.633  1.034
```

The flash is exactly on frame 60. The freeze starts at frame 79 (frame 80 onward is identical to frame 79) and lasts 1.034 seconds.

**2. Which frame does the button appear on?**

```bash
pixelprobe find demo.mkv "first change in rect(240,150,160,60)"
```

```text
Name     Condition  Start  End  Start s  End s  Value
@match1  change     29     30   0.967    1.000  96.6667
```

The button region first changes between frames 29 and 30, so the button appears on frame 30 (1.000 s).

**3. Check the button color**

```bash
pixelprobe pixel demo.mkv --at "f=30 point(300,180)"
```

```text
Pixel query (frame 30)
x    y    R   G   B    HEX
300  180  40  90  220  #285ADC
```

The value matches the (40, 90, 220) defined in the generator script exactly. Add `--json` to any command for structured output that can be used directly in automated test assertions; returned names such as `@match1` can be referenced in a later command's `--at`.

An AI calling through MCP uses the same syntax, for example `pixelprobe_find(media_path="demo.mkv", query="flash | freeze >= 0.5s")`; parity tests guarantee identical CLI and MCP results.

## Example Result: Digits Hidden in Noise

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

**The easiest way is the plugin**: both Claude Code and Codex can install it straight from this repository. It bundles the MCP server with five scenario Skills (UI recordings, long videos, quality, forensics, and the general workflow); see [docs/plugins.md](docs/plugins.md). Manual configuration follows.

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

MCP provides 10 tools that together cover every CLI feature (see the mapping in [MCP tools and jobs](docs/mcp-jobs.md)). Work coarse to fine: `pixelprobe_overview` for the big picture → `pixelprobe_find` to locate moments → `pixelprobe_look` to view annotated images → `pixelprobe_measure` for exact values. Locations go in the `at` parameter using the same syntax as the CLI `--at`; returned names such as `event1` or `seg3` can be reused in a later call as `at="@event1"`. MCP also exposes the `pixelprobe_analyze_media` Prompt and the `pixelprobe://guidance` Resource, which reinforce the principle that visual understanding is primary and deterministic data is supporting evidence.

For images, the response separately describes stored sample channels, the RGB channels used for deterministic analysis, and the visual PNG channels. It also reports palette usage, complete alpha statistics, and candidates for regular high-frequency textures. A texture candidate includes the analyzed coverage, period, and correlation evidence. It is only a prompt for agent review and is never presented as confirmed compression, corruption, or manipulation.

Images returned to the agent carry original-coordinate rulers, can overlay an 8×6 grid (`grid=true`; cell names can be used directly as `cell(C4)`), and are scaled to `max_side`; the scale factors are included in the response. Previews are for locating things only — read exact values with `pixelprobe_measure`. Operations that write Bundles (evidence, workflows, representation generation) write only to the controlled Artifact directory and never overwrite an existing result.

### Memory Use

Memory is sized automatically from the machine, so usually nothing needs configuring: the total budget is the smaller of half the physical memory and 70% of the memory free at startup, and each task may use up to "total budget ÷ number of workers". A machine with 32 GB of RAM gets about 8 GB, enough to analyze 2K and 4K video directly.

- To cap or fix the amount, set `PIXELPROBE_MAX_MEMORY_MB` (shared by the CLI and MCP) or `PIXELPROBE_MCP_TOTAL_MEMORY_MB` (MCP only); plugin users fill in the plugin's memory-budget setting.
- A task that fails for lack of memory waits for other tasks to finish and is rerun once with the whole budget; the job status field `memory_retry` records why.
- When free system memory is very low, heavy tasks wait before starting so the computer is not slowed down.

See [resource control](docs/resource-control.md) (Chinese) for details.

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

Then restart the MCP-compatible agent or desktop client and call `pixelprobe_system` (action=capabilities) to confirm `pixelprobe_version`.

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
| `track` | Track a point frame by frame: path, displacement, speed, and a follow slice (see [docs/slices.md](docs/slices.md)) |
| `streams` | Video container and coding structure: tags, keyframe intervals, timestamp gaps, bytes per second, and forensic clues (see [docs/streams.md](docs/streams.md)) |
| `find` | Search with a query for black/white frames, flashes, cuts, freezes, dropped-frame gaps, similar frames, metric conditions, and region changes (see [docs/find.md](docs/find.md)) |
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

## Running the Tests

Install the development dependencies from source and run the full suite (about 600 tests, roughly two minutes on a typical computer):

```bash
python -m pip install -e ".[dev,mcp,flow,storage,evidence]"
python -m pytest
```

To run only part of it, pass a file or a keyword, for example:

```bash
python -m pytest tests/test_capability_parity.py
python -m pytest -k "memory_retry"
```

What the main test files cover:

| Test file | What it checks |
|---|---|
| `test_capability_parity.py` | Every feature works in both the CLI and MCP, with identical values for the same input |
| `test_resource_enforcement.py`, `test_mcp_jobs.py`, `test_mcp_worker.py` | Memory and disk budgets, timeouts, cancellation, retry after failure, worker cleanup |
| `test_error_semantics.py` | Clear error codes for missing, corrupt, or duplicate-output cases |
| `test_evidence.py`, `test_c2pa_real.py` | EXIF, ELA, noise, and C2PA signature verification |
| `test_v10_generate.py`, `test_v08_bundle.py` | Structure, integrity, and values of the unified representation engine and result bundles |

After changing computation logic, also confirm that CLI output still matches the numeric baseline:

```bash
python scripts/capture_golden.py --check
```

It passes when every case matches the baseline. If expected values genuinely need to change, refresh them with `--write` and explain why in the commit message.

## License

PixelProbe is licensed under the [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0). Use, modification, and distribution are subject to the full terms in this repository's `LICENSE` file. Third-party dependencies remain subject to their respective licenses.

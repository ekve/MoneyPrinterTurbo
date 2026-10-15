# Storyboard Project v1

A portable project is a ZIP archive containing `storyboard.json` and optional
PNG/JPG files in a flat `assets/` directory. No manual copying into Streamlit is
required.

## Example

```text
taxesai-demo.zip
├── storyboard.json
└── assets/
    ├── homepage.png
    └── accountant-workspace.png
```

`storyboard.json`:

```json
{
  "format": "moneyprinterturbo-storyboard",
  "version": 1,
  "title": "Taxes AI for accountants",
  "video_aspect": "16:9",
  "voice": "en-IE-EmilyNeural",
  "scenes": [
    {
      "id": "scene-01",
      "type": "generated_image",
      "prompt": "Realistic Irish taxpayer reviewing paperwork, premium fintech advertising",
      "voiceover": "Tax paperwork takes time. Finding the right information should not.",
      "duration": "auto"
    },
    {
      "id": "scene-02",
      "type": "local",
      "file": "assets/homepage.png",
      "voiceover": "Taxes AI brings documents and information together.",
      "duration": 6
    },
    {
      "id": "scene-03",
      "type": "local",
      "file": "assets/accountant-workspace.png",
      "voiceover": "Accountants receive better organised client information.",
      "duration": 7
    }
  ]
}
```

## Using the editor

1. Select **Storyboard (AI + uploaded images)** in **Video Source**.
2. Choose a Storyboard ZIP and press **Import ZIP**.
3. The editor restores ordered scenes, prompts, scene narration text, timing
   mode and local image uploads.
4. Make changes and press **Prepare ZIP export**, then **Download Storyboard
   ZIP** for an updated portable copy.

Stage 2 adds **per-scene TTS narration**. When Storyboard scenes contain
`voiceover`, those texts are authoritative over the general Video Script.
The narration service synthesises a separate audio file for each voiced scene,
joins them into `audio.wav` (including silence for scenes without narration),
and saves their measured boundaries to `scene-timeline.json` alongside the task.

- `duration: "auto"` now uses the real measured TTS length, plus a short
  0.1-second tail. Empty auto scenes fail with an actionable error.
- Manual durations are minimums: narration is not truncated if it is longer.
- A scene exceeding 30 seconds fails rather than clipping speech. Split the
  narration into multiple scenes or shorten the text.
- The selected voice in **Audio Settings** is used for each scene. Imported
  project voice is selected automatically when supported.
- **Audio Settings → Automatic Voiceover** is required. A separately uploaded
  full-track narration continues using the original pipeline.
- The existing subtitle provider uses sentence-level offsets with scene
  boundaries. Word-level timing is not claimed to be accurate yet.

Stage 3 adds a dedicated **visual timeline compositor** for Storyboard.

- Each scene is shown exactly once, in the uploaded order. No stock-material
  looping, random shuffling, or duplicate filler clips are used.
- The compositor reads `scene-timeline.json` and cuts each image at that
  scene's narration boundary. The image sources include extra source frames,
  avoiding blank frames near fractional-second cuts.
- Without per-scene audio, scenes must use Manual timing. A mismatch between
  the total manual visual duration and the actual audio length is reported
  rather than silently repeating pictures.
- Stock video providers still use their original montage algorithm.
- Storyboard playback-speed adjustments and transition effects are currently
  ignored to preserve exact scene cuts. Use the default 1.0x speed and None
  transition for synchronized storyboard renders.

The editor shows estimated durations until TTS has been generated. Accurate
timing is available only after scene narration generation completes.

The editor now shows **Actual timing from TTS** beneath the storyboard:

- Per scene: measured voiceover length, final on-screen duration, and
  start/end timestamps in the combined narration timeline.
- Overall: measured joined audio length and calculated scene timeline length.
- The panel refreshes automatically while TTS is being generated.
- After a page restart, the editor can locate a previous report whose
  narration text, order and timing modes match the open storyboard.
- Editing the scene narration or reordering scenes hides unrelated timings
  until a matching render exists. A previous render is identified by task ID.
- "Actual" means **measured TTS + planned scene cuts**, not independent
  verification of the final encoded MP4's exact duration. Small differences
  due to FPS rounding are expected.

Validation tests:

```powershell
docker compose exec webui python -m pytest test/services/test_storyboard_timing_display.py -q
```


To verify:
```powershell
git pull
docker compose restart webui
docker compose exec webui python -m pytest test/services/test_storyboard_timeline.py test/services/test_storyboard_narration.py test/services/test_storyboard.py test/services/test_storyboard_project.py -q
```

On import, the editor combines scene `voiceover` fields into the existing
**Video Script**, selects the requested TTS voice when available, and restores
the **Video Subject**.

## Validation / safety

- Archive size: up to 60 MB.
- Manifest size: up to 256 KB.
- Maximum scenes: 50. Maximum image assets: 50, PNG/JPG only.
- Local files must be stored at `assets/<filename>.png`,
  `assets/<filename>.jpg` or `assets/<filename>.jpeg`.
- HTTP links, absolute paths, nested directories, traversal, symlinks and
  unrelated archive members are rejected.
- Images go through the normal verified MoneyPrinterTurbo local upload flow.
- Failed imports clean up files saved earlier in the same import.
- Exporting a local image requires it to exist in the verified uploads directory.

## Tests

```powershell
git pull
docker compose exec webui python -m pytest test/services/test_storyboard_project.py test/services/test_storyboard.py test/services/test_storyboard_narration.py -q
```

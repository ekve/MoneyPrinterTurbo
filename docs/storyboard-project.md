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

In stage 1, `voiceover` and `voice` are stored as project metadata but
**not yet used for per-scene TTS**. `duration: "auto"` imports with a 5-second
preview duration. Real TTS-based scene synchronization is a separate next stage.
On import, the editor combines scene `voiceover` fields into the existing
**Video Script**, selects the requested TTS voice when available, and restores
the **Video Subject**. The current **Video Script** and **Audio Settings** still
control actual narration. Do not treat an imported ZIP as a finished
synchronized video.

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
docker compose exec webui python -m pytest test/services/test_storyboard_project.py test/services/test_storyboard.py -q
```

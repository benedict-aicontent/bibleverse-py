# Verse Shorts Automation

One command builds a batch of 7 vertical videos (1080x1920, 30 fps) from `Batch.md`.

## Folder layout (everything lives in one folder)

```
VerseShorts/
  make_videos.py      <- the automation
  config.json         <- settings (optional changes)
  look.json           <- random fonts / colours / styles
  Batch.md            <- your batch (same format every time)
  backgrounds/        <- images AND/OR video clips (one folder)
  music/              <- mp3 / wav / m4a tracks
  voices/             <- Piper voice model (step 3)
  fonts/              <- optional: your own .ttf/.otf files
  output/             <- finished videos + .txt files appear here
```

## One-time setup (Windows)

1. Open a terminal in the folder (type `cmd` in the folder's address bar).
2. Install the Python packages:
   ```
   pip install pillow piper-tts
   ```
3. Download a free offline voice into the `voices` folder:
   ```
   python -m piper.download_voices en_US-ryan-high --data-dir voices
   ```
   You should now have `voices\en_US-ryan-high.onnx` and `voices\en_US-ryan-high.onnx.json`.
   (Manual download: huggingface.co/rhasspy/piper-voices, `en/en_US/ryan/high`. Other voices such as
   `en_US-lessac-medium` or `en_GB-alan-medium` work too - just change `piper_model` in `config.json`.)
4. Put images and/or video clips in `backgrounds`, and music in `music`.
5. Check that `ffmpeg -version` works in the terminal (you said it's installed).

**No Piper?** Use Windows' built-in voices instead: `pip install pyttsx3`, then in `config.json` set
`"tts": { "engine": "windows", "windows_voice_contains": "David" }`. Lower quality, zero downloads.

## Every batch

1. Drop the new `Batch.md` next to `make_videos.py`.
2. Run:
   ```
   python make_videos.py
   ```
3. Collect `output\Day1_James_5-15.mp4` and the matching `.txt` (title, description, tags) for each day.

Handy options: `--days 2,5` (only those days), `--seed 42` (repeat the same random choices),
`--keep-temp` (keep the `work` folder for debugging). A failed day is logged to `output\run_log.txt`
and the rest of the batch continues.

## What it does, per day

| Part | Behaviour |
|---|---|
| Voice | Hook, then "James 5, verse 15" + verse, then reflection; 0.6 s pauses, plus a 0.9 s pause after the reference; starts at 2 s |
| Length | Voice length (incl. pauses) + 10 s, i.e. 2 s lead-in + 8 s after the voice ends |
| Text | Three whole blocks (hook / verse with reference / reflection) fade in and out with the voice |
| Look | Random font set + colour set + style from `look.json` (one look per video) |
| Image background | One image for the whole video; never reused inside a batch |
| Clip background | Trims of 4 s, 4 s, 4 s, 8 s repeating; no clip reused inside one video; 8 s trims get a slow zoom-in and a transition (one random transition type per video) |
| Mixed folder | If the folder has both images and clips, each video randomly gets one or the other |
| Music | Random track per video, none repeated across the batch; half the voice volume, ducked further while the voice speaks; fades in and out |
| Clip audio | Always muted |

## Tuning (config.json)

* `voice_speed`: 0.92 = a calmer pace, 1.08 = brisker.
* `pause`: gap between hook / verse / reflection.
* `reference_pause`: gap between the spoken reference ("James 5, verse 15") and the verse text.
* `pronunciations`: fix a word, e.g. `{"Yahweh": "Yah-weh"}` (spoken audio only; on-screen text is unchanged).
* `music.relative_volume`: 0.5 = half the voice. `music.duck`: false turns the extra dip off.
* `image_slow_zoom`: true adds a slow zoom to still-image backgrounds (slower to render).
* `clips.pattern`, `clips.zoom_max`, `clips.transition_duration`: trim lengths, zoom amount, transition length.
* Fonts: `look.json` uses Windows fonts by file name (Georgia, Times, Palatino, Constantia, Segoe UI, Calibri,
  Trebuchet, Arial). A font set whose files are missing is skipped. Add your own by putting the file in `fonts`.

## Notes

* Add at least 7 images (or 7 clips for the no-repeat rule to hold across a batch) and 7 music tracks.
  With fewer, the script reuses some and says so in the log.
* Each video needs roughly 1-3 minutes to render, depending on your PC.
* Clips shorter than the trim they are asked for are looped; keep clips at 10 s or longer for best results.

run in environment
.\.venv\Scripts\Activate.ps1

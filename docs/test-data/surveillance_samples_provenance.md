# Surveillance Test Clips — Provenance

These are small, realistic surveillance/CCTV-like 10-second clips used as test
fixtures for motion detection / high-frequency event sanity checks. They replace
the misleading synthetic `testsrc` signal for motion-calibration purposes (real
CCTV motion is small and localized, unlike full-frame synthetic motion).

> NOTE ON VERSIONING: `data/` is gitignored in this repo, and existing test videos
> are intentionally NOT committed. These `.mp4` files are therefore LOCAL fixtures
> only — they are not committed/redistributed. This provenance file is mirrored at
> the tracked path `docs/test-data/surveillance_samples_provenance.md`, and the
> reproducible fetch/cut script is at `scripts/fetch_surveillance_samples.sh`, so
> anyone can regenerate the exact clips without the binaries being in git.

## Source Dataset

- Dataset: VIRAT Video Dataset, Ground Camera, Release 2.0
- Project page: https://viratdata.org/
- Public host: Kitware Girder — https://data.kitware.com/
  (collection `56f56db28d777f753209ba9f` → Public Dataset → Release 2.0 →
  VIRAT Ground Dataset → `videos_original`)
- Access: public, no login/token/CAPTCHA. Per-file download endpoint:
  `https://data.kitware.com/api/v1/item/{itemId}/download`
- Usage/License: VIRAT Ground Camera data is provided under the "VIRAT Video
  Dataset Protection Agreement" (research use), see
  https://viratdata.org/resources/VIRAT-Video-Data-Set-Protection-Agreement-1-4-11.pdf
  We keep only short local fixtures (not committed, not redistributed), which is
  consistent with research/evaluation use. If you intend to redistribute or use
  these beyond local testing, review the agreement first.
- Citation: Sangmin Oh et al., "A Large-scale Benchmark Dataset for Event
  Recognition in Surveillance Video", CVPR 2011.

## Clips

All clips: cut with `ffmpeg -ss 00:00:02 -t 10`, re-encoded H.264
(`libx264 -crf 23 -pix_fmt yuv420p`), audio dropped (`-an`). Verified 1280x720,
~23.97 fps, 240 frames, duration ≈ 10.01s.

### 1. surveillance_virat_parking_10s.mp4
- Source item id: `56f588218d777f753209ccde`
- Original filename: `VIRAT_S_010208_07_000768_000791.mp4`
- Original: 1280x720, ~23.97 fps, 19.23s, 1,362,385 bytes
- Cut command: `ffmpeg -ss 00:00:02 -t 10 -i VIRAT_S_010208_07_000768_000791.mp4 -c:v libx264 -crf 23 -pix_fmt yuv420p -an surveillance_virat_parking_10s.mp4`
- Result: 10.012516s, 1280x720, 240 frames, 574,683 bytes
- Motion (project FrameDiffMotionDetector default 4fps/128x72): peak 0.0062, mean 0.0042
- Why useful: stationary parking-lot surveillance with intermittent vehicle/person
  movement — highest motion of the three; good for a "motion present" sanity case.

### 2. surveillance_virat_pedestrian_10s.mp4
- Source item id: `56f5879c8d777f753209cb34`
- Original filename: `VIRAT_S_010003_07_000608_000636.mp4`
- Original: 1280x720, ~23.97 fps, 20.65s, 1,618,350 bytes
- Cut command: `ffmpeg -ss 00:00:02 -t 10 -i VIRAT_S_010003_07_000608_000636.mp4 -c:v libx264 -crf 23 -pix_fmt yuv420p -an surveillance_virat_pedestrian_10s.mp4`
- Result: 10.012516s, 1280x720, 240 frames, 619,853 bytes
- Motion: peak 0.0036, mean 0.0010
- Why useful: low/sparse pedestrian motion in an open scene — a "low motion"
  calibration case (small, localized change).

### 3. surveillance_virat_street_10s.mp4
- Source item id: `56f587aa8d777f753209cb70`
- Original filename: `VIRAT_S_010005_05_000397_000430.mp4`
- Original: 1280x720, ~23.97 fps, 23.28s, 1,639,520 bytes
- Cut command: `ffmpeg -ss 00:00:02 -t 10 -i VIRAT_S_010005_05_000397_000430.mp4 -c:v libx264 -crf 23 -pix_fmt yuv420p -an surveillance_virat_street_10s.mp4`
- Result: 10.012516s, 1280x720, 240 frames, 570,008 bytes
- Motion: peak 0.0038, mean 0.0010
- Why useful: ground-level street/scene background with occasional movers — another
  low-motion case with a different background, useful to confirm the detector does
  not over- or under-trigger across scenes.

## Reproduce

```bash
bash scripts/fetch_surveillance_samples.sh
```

This downloads the three source videos from the public Girder endpoints into a temp
dir, cuts the 10s clips into `data/test_videos/surveillance_samples/`, and ffprobes
each. Total download ≈ 4.6 MB (well under any dataset size limit).

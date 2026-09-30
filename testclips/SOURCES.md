# Test clip sources

All clips downloaded 2026-09-28 from Pexels under the [Pexels licence](https://www.pexels.com/license/):
free for commercial use, modification allowed, attribution not required. Not allowed: selling unaltered
copies, redistributing as stock, showing identifiable people in a bad light, implying endorsement.

Byte counts were checked against the server's Content-Length before and after download.

| File | Pexels page | Creator | Probed | Bytes |
|---|---|---|---|---|
| 01_crowd_tokyo_night_18662635.mp4 | https://www.pexels.com/video/a-group-of-people-walking-through-a-crowded-city-street-18662635/ | Cheng | 1920x1080, 60 fps, 5.40 s, 320 frames | 4521522 |
| 02_small_faces_manchester_5021553.mp4 | https://www.pexels.com/video/crowd-of-people-walking-on-the-streets-5021553/ | zahid Anwar | 1920x1080, 29.97 fps, 14.85 s, 445 frames | 9593107 |
| 03_profile_london_night_6093091.mp4 | https://www.pexels.com/video/people-crossing-the-pedestrian-6093091/ | George Morina | 1280x720 (URL says 1080), 29.97 fps, 28.71 s, 860 frames | 11058964 |
| 04_masks_hongkong_4562551.mp4 | https://www.pexels.com/video/people-wearing-face-mask-in-public-area-4562551/ | Suika Chan | 1280x720, 29.97 fps, 21.09 s, 632 frames | 7235703 |
| 05_plate_carpark_berlin_37508953.mp4 | https://www.pexels.com/video/urban-parking-scene-with-cars-and-motorbike-37508953/ | Ivan Chumak | 2560x1440, 59.94 fps, 18.32 s, 1098 frames | 15760690 |
| 06_plate_night_16768845.mp4 | https://www.pexels.com/video/the-rear-end-of-a-car-at-night-16768845/ | Erik Mclean | 1920x1080, 59.94 fps, 12.46 s, 744 frames | 1235466 |
| 07_office_screens_7581202.mp4 | https://www.pexels.com/video/people-working-on-office-using-computer-7581202/ | RDNE Stock project | 1920x1080, 30 fps, 13.57 s, 407 frames | 7798579 |
| 08_talking_head_5442623.mp4 | https://www.pexels.com/video/person-having-a-interview-5442623/ | Tima Miroshnichenko | 1920x1080, 25 fps, 21.16 s, 529 frames | 8324804 |

Direct file URLs (videos.pexels.com):

- 01 `https://videos.pexels.com/video-files/18662635/18662635-hd_1920_1080_60fps.mp4`
- 02 `https://videos.pexels.com/video-files/5021553/5021553-hd_1920_1080_30fps.mp4`
- 03 `https://videos.pexels.com/video-files/6093091/6093091-hd_1920_1080_30fps.mp4`
- 04 `https://videos.pexels.com/video-files/4562551/4562551-hd_1280_720_30fps.mp4`
- 05 `https://videos.pexels.com/video-files/37508953/15891530_2560_1440_60fps.mp4`
- 06 `https://videos.pexels.com/video-files/16768845/16768845-hd_1920_1080_60fps.mp4`
- 07 `https://videos.pexels.com/video-files/7581202/7581202-hd_1920_1080_30fps.mp4`
- 08 `https://videos.pexels.com/video-files/5442623/5442623-hd_1920_1080_25fps.mp4`

Local testing only. Redacted versions may be used in demos later (modified, per the licence).

## Derived test file

- `09_talking_head_with_audio_test.mp4` — clip 08's video stream copied unchanged, plus a generated
  440 Hz tone as AAC audio. Made locally to test that exports carry audio through (the Pexels clips have none).

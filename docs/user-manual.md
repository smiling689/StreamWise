# StreamWise User Manual

## 1. Environment Setup

StreamWise runs on Linux, including WSL2 with WSLg. The conda environment provides Python
3.12, FFmpeg/ffplay, and the sqlite3 command-line tool, so no root access is needed.

```bash
conda env create -f environment.yml
conda activate streamwise
pytest            # about 15 s; all tests should pass
```

After `environment.yml` changes:

```bash
conda env update -f environment.yml --prune
```

Optional system tools:

| Tool | Purpose | Install |
| --- | --- | --- |
| Wireshark / tshark | Packet capture (T-04) | `sudo apt install tshark`, then `sudo usermod -aG wireshark "$USER"` and log in again |

Run every command below from the repository root with the environment activated.

## 2. Preparing Media

Download the source video (Big Buck Bunny, CC BY 3.0, 275 MB) and segment 180 s of it. This
takes about 2 minutes on a multi-core machine:

```bash
mkdir -p media/raw
curl -L -o media/raw/bbb.zip https://download.blender.org/demo/movies/BBB/bbb_sunflower_1080p_30fps_normal.mp4.zip
unzip media/raw/bbb.zip -d media/raw && rm media/raw/bbb.zip
streamwise-media segment media/raw/bbb_sunflower_1080p_30fps_normal.mp4 --name bbb --start 30 --duration 180
```

This creates `media/bbb/` with 36 × 6 segments, `manifest.json`, and `quality.json`
(per-segment PSNR). Any video works. Useful options:

| Option | Default | Meaning |
| --- | --- | --- |
| `--name` | (required) | Video name used in file names (letters, digits, `_`) |
| `--segment-duration` | 5 | Seconds per segment; must be a whole number of frames |
| `--start`, `--duration` | 0, all | Portion of the source to use |
| `--ladder` | `240:400,360:800,480:1200,720:2500,1080:5000,1080:8000` | `HEIGHT:KBPS` levels |
| `--preset` | `fast` | x264 preset |
| `--no-quality` | off | Skip the PSNR measurement (run `streamwise-media quality media/<name>` later) |

`media/` is git-ignored, because it can always be regenerated.

## 3. Running the Server

```bash
streamwise-server                                  # port 9000, no bandwidth limit
streamwise-server --rate-mbps 4                    # constant 4 Mbit/s per client
streamwise-server --trace traces/drop.txt          # bandwidth trace, per client
streamwise-server --trace traces/demo_shared.txt --shaping-scope global   # one shared link
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--host`, `--port` | `0.0.0.0`, `9000` | Listen address |
| `--media-dir` | `media` | Directory with segmented videos |
| `--log-db` | `logs/server.db` | SQLite transmission log |
| `--rate-mbps` / `--trace` | none | Constant limit or bandwidth trace file |
| `--shaping-scope` | `connection` | `connection`: independent limit per client, trace starts at connect; `global`: one limit shared by all clients, trace starts at server start |
| `--burst-kb` | 32 | Token-bucket size |
| `--encryption` | `optional` | `off`, `optional` (encrypt when the client asks) or `required` (reject plaintext clients) |
| `--psk-file` | none | Pre-shared key file; clients must use the same key |

A trace file holds `<time_s> <mbps>` lines; `#` starts a comment, and each rate holds until
the next line. `streamwise-experiment gen-trace FILE --seed N` generates a fluctuating trace.

Inspect the transmission log:

```bash
sqlite3 -header -column logs/server.db "SELECT segment, bitrate_kbps, bytes, round(duration_s,2) AS s, round(throughput_bps/1e6,2) AS mbps, status, datetime(start_time,'unixepoch','localtime') AS sent FROM transmissions ORDER BY id DESC LIMIT 10"
```

Export it as CSV, e.g. for a spreadsheet:

```bash
sqlite3 -header -csv logs/server.db "SELECT * FROM transmissions" > transmissions.csv
```

## 4. Running a Client

```bash
streamwise-client --name client1                       # buffer-based ABR, ffplay window
streamwise-client --name client2 --abr throughput --mute
streamwise-client --name probe --player none --segments 6   # headless, first 6 segments
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--host`, `--port` | `127.0.0.1`, `9000` | Server address |
| `--video` | `bbb` | Video name |
| `--abr` | `buffer` | `throughput`, `buffer`, `qlearning`, or `fixed:<level\|min\|max>`; parameters as in `throughput:window=5,safety=0.9` or `buffer:reservoir_s=5,cushion_s=15` |
| `--max-buffer` | 30 | Buffer cap (s) |
| `--startup-buffer` | 5 | Media buffered before playback starts (s) |
| `--segments` | all | Only stream the first N segments |
| `--player` | `ffplay` | `none` runs headless |
| `--mute` | off | No audio |
| `--window-size`, `--window-pos` | `640x360`, auto | Player window geometry |
| `--download-dir` | `download` | Segment cache root |
| `--log-db` / `--no-log` | `logs/client.db` | SQLite session log |
| `--encrypt` | off | Require an AES-256-GCM encrypted connection |
| `--psk-file` | none | Pre-shared key file; authenticates the server (implies `--encrypt`) |

The client prints one line per segment (level, measured throughput, download time, buffer
level, stalls), marks `UP`/`DOWN` switches, and ends with a summary. Each segment is stored
in `download/<session>/` until it has been played, and the directory is removed when the
client exits, including after Ctrl-C.

### Encrypted Streaming (extension E-02)

```bash
openssl rand -hex 32 > secret.psk                  # shared key (keep it out of git)
streamwise-server --encryption required --psk-file secret.psk
streamwise-client --name client1 --psk-file secret.psk
```

`--encrypt` without a key also works; it protects against eavesdropping but not against an
active man-in-the-middle (see [encryption.md](encryption.md)). A client with a different key
fails with `key confirmation failed`, and a plaintext client gets `server error 426`. The
`encryption` column of the server's `sessions` table records what each connection used.
`streamwise-experiment crypto-bench` measures the encryption cost, and
`streamwise-experiment run experiments/encryption.toml` repeats the main evaluation encrypted.

## 5. Live Demo

```bash
scripts/demo.sh                       # 3 clients, shared 30 -> 4 -> 30 Mbit/s link
SEGMENTS=8 scripts/demo.sh            # short rehearsal
scripts/demo.sh traces/drop_recover.txt 9001
```

The script starts a server with `--shaping-scope global` and three client windows side by
side: client 1 uses throughput-based ABR (with sound), client 2 buffer-based ABR, and client 3
the non-adaptive baseline `fixed:3` (always 720p); clients 2 and 3 are muted. During the drop
the adaptive clients switch down while the fixed client stalls. Logs go to `logs/demo-<time>/`. Press
Ctrl-C to stop everything.

## 6. Experiments and Report

```bash
streamwise-experiment run experiments/smoke.toml      # ~40 s pipeline check
streamwise-experiment run experiments/default.toml    # full evaluation, ~8-10 min
streamwise-experiment report results/<dir>            # rebuild the report
```

An experiment runs one server per network scenario and one headless client process per
ABR, all in parallel. The output directory contains `summary.md` / `summary.csv`,
`figures/metrics.png`, `figures/timeline-<scenario>.png`, the SQLite logs, and the logs of
every process. To define new scenarios and algorithms, copy `experiments/default.toml`.

### Q-learning ABR (extension E-01)

The trained model `models/qlearning-bbb.json` is part of the repository; use it with
`--abr qlearning`. To retrain or re-evaluate it, first download the measured 3G traces:

```bash
scripts/fetch_hsdpa_traces.sh                     # -> traces/hsdpa (git-ignored)
streamwise-sim train --seed 1 --out models/qlearning-bbb.json
streamwise-sim evaluate --abr throughput --abr buffer --abr qlearning --reference qlearning
streamwise-sim validate results/default           # simulator vs. a real experiment
streamwise-experiment run experiments/qlearning.toml
```

The model is tied to the bitrate ladder it was trained on; with other media, retrain it. See
[intelligent-abr.md](intelligent-abr.md) for the method and the results.

## 7. Packet Capture with Wireshark

```bash
scripts/capture.sh 9000 logs/session.pcapng     # in one terminal; Ctrl-C to stop
streamwise-client --segments 4                  # in another
tshark -X lua_script:tools/wireshark/streamwise.lua -r logs/session.pcapng -Y streamwise
```

For the Wireshark GUI, copy `tools/wireshark/streamwise.lua` into the personal plugins
folder (*Help > About Wireshark > Folders*) and restart Wireshark. The filter `streamwise`
then shows decoded messages, e.g. `GET_SEGMENT id=3 bbb-1080p-8000k-2.ts`. On an encrypted
connection only `HELLO` and `WELCOME` are readable; later messages appear as, e.g.,
`GET_SEGMENT [encrypted] id=3 (35 bytes)` with their ciphertext and GCM tag. Use
*Statistics > I/O Graphs* or *Statistics > TCP Stream Graphs* to show throughput changes
during a bandwidth drop.

## 8. Troubleshooting and Player Debugging

| Symptom | Cause / fix |
| --- | --- |
| `ffplay not found on PATH` | The conda environment is not activated: `conda activate streamwise`. |
| No player window | Check `echo $DISPLAY` (WSLg sets `:0`). Test the player alone: `ffplay -f lavfi -i testsrc`. Over SSH without X, use `--player none`. |
| Audio errors or no sound | No audio device is available (e.g. remote session): use `--mute`. Under WSLg, PulseAudio is provided at `/mnt/wslg/PulseServer`. |
| Player window closed early | The client logs a warning and continues headless; metrics are unaffected. |
| Video shows artefacts after a switch | Check the media with `ffmpeg -v warning -i <seg-a.ts> ...`; segments made by `streamwise-media` decode cleanly when mixed (see `tests/test_media.py`). Re-segment media made by other tools. |
| `address already in use` | Another server is on the port: `pgrep -af streamwise-server`, or pass `--port`. |
| `connecting to ... timed out` / `ConnectionRefusedError` | The server is not running, or the host/port is wrong; check the firewall when crossing machines. |
| `missing segment` / `size ... != manifest size` at server start | The media directory is incomplete or modified; re-run `streamwise-media segment`. |
| `tshark: Couldn't run dumpcap ... Permission denied` | Add yourself to the `wireshark` group (see §1) and log in again. |

To debug the player in isolation, feed it segments by hand:

```bash
cat media/bbb/bbb-360p-800k-0.ts media/bbb/bbb-1080p-8000k-1.ts | ffplay -i pipe:0
```

# Speech to text — whisper on the CPU

`stacks/stt/` runs [whisper.cpp](https://github.com/ggml-org/whisper.cpp)'s server on ladybird's
CPU at `http://192.168.0.13:8990`. It exists for one consumer: the dashboard's assistant — the mic
button records in the browser, the dashboard server forwards the audio here, and the transcript is
sent to the LLM on snibbly. Nothing leaves the LAN.

| | |
|---|---|
| **Model** | `ggml-small.en-q5_1.bin` (~190 MB) — English-only, quantised; good accuracy for short voice commands |
| **Speed** | a few-second command transcribes in well under a second on the i5-12600H (6 threads) |
| **API** | `POST /inference`, multipart field `file`, returns `{"text": "..."}`. `--convert` means webm/mp4/wav all work. |

```bash
# smoke test with the sample baked into the image
docker exec whisper-stt sh -c 'cp /app/samples/jfk.wav /tmp/' \
  && docker cp whisper-stt:/tmp/jfk.wav /tmp/ \
  && curl -s http://192.168.0.13:8990/inference -F file=@/tmp/jfk.wav | head -c 200
```

## Model download (rebuild step)

The model file is not in git. Fetch it once into the stack's `models/` dir:

```bash
mkdir -p /opt/stacks/stt/models
curl -L -o /opt/stacks/stt/models/ggml-small.en-q5_1.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en-q5_1.bin
```

Want better accuracy at ~2× the latency? `ggml-medium.en-q5_0.bin`. Faster and dumber:
`ggml-base.en.bin`. Swap the filename in the compose command to match.

## The one gotcha: microphones need HTTPS

Browsers only expose `getUserMedia` in a secure context, and the dashboard is served over plain
http — so the mic button works via `http://localhost` (dev) or an HTTPS front like
`tailscale serve` (`https://ladybird.<tailnet>.ts.net`), but **not** via `http://ladybird/`. The
STT service itself doesn't care; this is purely a browser rule about the page that's recording.

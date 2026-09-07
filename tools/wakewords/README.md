# Bundled wake-word models

`hey_hermes.tflite` — the on-device "Hey Hermes" hotword model. This is the
default detector for the wake word feature (see
`website/docs/user-guide/features/wake-word.md`); no training or setup is
required to say "hey hermes".

- **Engine:** [pyopen-wakeword](https://github.com/rhasspy/pyopen-wakeword)
  (rhasspy's maintained fork of openWakeWord; Apache-2.0). Runs TFLite via a
  bundled `tensorflowlite_c` library — no onnx, no runtime download.
- **Provenance:** trained with the openWakeWord training pipeline (synthetic
  TTS-generated speech), which produces the `.tflite` artifact. Redistribution
  is permitted under the openWakeWord license.
- **Label:** the model registers as `hey_hermes` (matches the filename).
- **Runtime:** the shared feature-extraction models (melspectrogram +
  embedding) are bundled inside the `pyopen-wakeword` wheel — byte-identical
  to the official openWakeWord v0.5.1 files, so scores match the original
  engine exactly.

To use a different phrase, train your own model and point
`wake_word.openwakeword.model` at its `.tflite` path. See the wake-word docs
for the training guide.

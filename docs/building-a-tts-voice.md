# Building your own TTS voice

This guide shows how to make a text-to-speech voice for a language no free service speaks well. Part 1 is how
anki-mcp's Classical Latin voice was built. Part 2 is the recipe for any language or voice.

```
text ──▶ eSpeak NG (rules: text → phonemes) ──▶ Piper / VITS (neural: phonemes → audio) ──▶ .wav ──▶ anki-mcp → .m4a
          "cīvitātēs" → kiːwɪtˈaːteːs              trained on one human reader
```

The split is the whole idea. **eSpeak** knows the pronunciation *rules* of 100+ languages but sounds robotic.
**Piper** is an open-source neural voice that turns eSpeak's phonemes into human-sounding audio. Train Piper on
recordings of one reader and you keep eSpeak's correct rules while getting a natural sound.

## Choose how much to build

Stop at the first option that works:

| Option | Effort | When |
|---|---|---|
| An existing free voice (Google via gTTS, macOS `say`, eSpeak) | minutes | the language has a real voice. Listen first: Google lists `la` but it reads Latin as English |
| A ready-made Piper voice ([voice list](https://huggingface.co/rhasspy/piper-voices)) | minutes | Piper already covers the language; drop the `.onnx` into `~/.local/share/piper-voices/` |
| **Fine-tune Piper on one reader** (this guide) | a day, ~5 GPU hours | no good voice exists, but eSpeak has rules for the language and recordings exist |
| Train from scratch | weeks | only to learn how TTS works; worse than fine-tuning |

## Part 1: The Latin voice

### Why it was needed

No free voice speaks Classical Latin: Google's `la` is English, Microsoft/Bing has none of 322 voices, and Meta's
MMS-TTS `lat` sounds Italian. eSpeak's `la` follows Classical rules (v said as /w/, c always hard) but is robotic.

### What was used

- **Data:** [Vox Classica](https://huggingface.co/datasets/Ken-Z/Latin-Audio) (CC-BY-4.0), 73 h of Classical Latin
  already cut into sentence clips with macronized text and a `speaker` column. A voice needs **one** speaker, so:
  speaker 12 (18 h of LibriVox: Aeneid, Georgics, Seneca, Lucretius, Caesar, Cato), chosen by listening to samples
  of the four largest. 3,204 clips, ~5.4 h, max 700 per work so the Aeneid's verse doesn't dominate.
- **Base model:** Piper's `en_US-lessac-medium` checkpoint (1.35 M training steps). The base language hardly matters:
  the phonemes are shared IPA symbols, and fine-tuning replaces the accent.
- **Compute:** a free Google Colab T4 GPU. A Mac has no CUDA; Piper training on its CPU runs ~0.2 steps/s, far too slow.
- **Steps:** Part 2's recipe: pick + download the clips, fine-tune (40 epochs ≈ 7 k steps at batch 16), export to
  ONNX, then `piper:la_LA-vox-medium` in anki-mcp.

### What went wrong (all caught with a 40-clip dry run on the Mac first)

1. **The old checkpoint won't load with `--ckpt_path`**: PyTorch ≥ 2.6 refuses its pickled `PosixPath`s. Use
   `--model.warmstart_ckpt`, which copies only the weights (better for fine-tuning anyway: you want the voice, not its
   old learning-rate schedule).
2. **Default checkpoints fill Google Drive**: Piper keeps 11 × 850 MB. The `train.py` in Part 2 keeps last + best.
3. **Every resume silently reset training.** Piper saves `warmstart_ckpt` inside each checkpoint; Lightning merges saved
   settings over the command line on resume; the warmstart then runs after the restore and overwrites the trained
   weights. No error, just a voice that never improves. The log said "Restored all states" with a
   `[warmstart] Copied 784 parameters` line in between. Part 2's `train.py` skips the warmstart when resuming; confirmed by
   comparing one weight tensor across base / epoch 1 / resumed epoch 2.
4. **ONNX export fails on torch ≥ 2.9** (new `dynamo` exporter can't trace VITS). Part 2's `export.py` forces the old exporter.
5. **macOS-only**: the Piper source build fails (eSpeak's `e_short_` vs `E_short_` collide on a case-insensitive disk;
   use the PyPI wheel), and DataLoader workers re-import the script (needs the `__main__` guard).

### Macrons matter at speaking time too

eSpeak takes Latin vowel length *and stress* from macrons: `cīvitātēs` → `kiːwɪtˈaːteːs`, `civitates` → `kɪwˈɪtatɛs`
(stress on the wrong syllable). The voice was trained on macronized text, but the Latin cards have none. So the deck
profile tells the agent to put macronized Latin in `audio.text` and leave Front as typed.

## Part 2: Recipe for any language

### 1. Check eSpeak has the language

```bash
brew install espeak-ng
espeak-ng --voices=<lang>                  # must list it
espeak-ng -v <lang> -q --ipa "a typical sentence"   # read the phonemes; they must be right
```

If the phonemes are wrong here, no amount of training fixes them: the network learns *sound*, not rules. Try the
language's spelling conventions (macrons, accents) and see which input gives correct phonemes; use that form for both
training text and speaking.

### 2. Get data: one speaker, 1-6 hours, clips with exact text

- **Best:** an existing sentence-aligned dataset (search Hugging Face for `<language> speech`, `tts`). Filter to one
  speaker; check the license allows training and note the attribution it requires.
- **Otherwise:** a public-domain audiobook (LibriVox) plus its text, cut into sentences. Align with Whisper
  timestamps + fuzzy matching to the known text, and spot-check boundaries.
- **Or record yourself:** read ~1,000 sentences (~1 h) in a quiet room. Most effort, your exact pronunciation.
- Clips 1-15 s, clean, no music, text exactly what is said. Drop clips with editorial marks (`[...]`) or odd characters.
- **Listen to samples before choosing a speaker.** Pronunciation traditions differ (e.g. Classical vs Ecclesiastical Latin).

Piper wants a folder of audio and a CSV: `clip.wav|Text of the clip.` It resamples to 22.05 kHz and trims silence itself.

### 3. Pick a base checkpoint

From [rhasspy/piper-checkpoints](https://huggingface.co/datasets/rhasspy/piper-checkpoints), a `medium` one (other
qualities need extra settings). `en_US/lessac/medium` is a robust default for any language.

**Licenses:** the base checkpoint's training data has a license, and so does yours. Lessac's
([Blizzard 2013](https://www.cstr.ed.ac.uk/projects/blizzard/2013/lessac_blizzard2013/license.html)) is research-only,
no commercial use, no redistribution. So a voice fine-tuned from it is fine for your own use, but don't publish or sell
it. To share a voice, start from a checkpoint whose data allows it and check your own data's license too (Vox Classica
is CC-BY-4.0: fine to share with credit). Sharing the *steps* is always fine.

### 4. Rehearse locally at toy scale

Install the trainer. The wheel ships its `monotonic_align` extension as source only, so build it once:

```bash
pip install "piper-tts[train]==1.8.0" onnxscript     # on macOS use the wheel: the source build fails on a case-insensitive disk
MA=$(python -c "import piper, os; print(os.path.dirname(piper.__file__))")/train/vits/monotonic_align
curl -sL -o $MA/core.pyx https://raw.githubusercontent.com/OHF-Voice/piper1-gpl/v1.8.0/src/piper/train/vits/monotonic_align/core.pyx
(cd $MA && mkdir -p monotonic_align && cythonize -q -i core.pyx && mv core*.so monotonic_align/)
```

Then run 40 clips for 1-2 epochs on CPU. Check each stage produces its artifact: cache →
checkpoint → **resume** (compare a weight tensor, don't trust the log) → ONNX export → a spoken `.wav`. Also check every
phoneme in your full dataset exists in the generated `phoneme_id_map`; Piper drops unknown ones silently.

### 5. Train on a GPU

Wrap Piper's trainer in a small `train.py` (fixes gotchas 2, 3 and 5 from Part 1):

```python
import torch
from lightning.pytorch.callbacks import ModelCheckpoint
from piper.train.__main__ import VitsDataModule, VitsLightningCLI, VitsModel

class Model(VitsModel):
    def on_fit_start(self):
        # warmstart_ckpt is saved in the checkpoint's hparams, so Piper would re-copy the base weights over the
        # restored ones on every resume. Only warmstart a fresh run.
        if self.trainer.ckpt_path:
            self._warmstart_ckpt = None
        super().on_fit_start()

if __name__ == "__main__":  # DataLoader workers re-import this module on macOS
    torch.backends.cuda.matmul.allow_tf32 = True
    checkpoint = ModelCheckpoint(monitor="val_mel", mode="min", save_top_k=1, save_last=True)
    VitsLightningCLI(Model, VitsDataModule, trainer_defaults={"max_epochs": -1, "callbacks": [checkpoint]})
```

and put the per-voice settings in `train.yaml`:

```yaml
model: {sample_rate: 22050, mos_metric: none}
data: {voice_name: <lang>_<REGION>-<name>-medium, espeak_voice: <lang>}
```

```bash
python train.py fit --config train.yaml --data.csv_path metadata.csv --data.audio_dir wavs \
  --data.cache_dir cache --data.config_path config.json --data.batch_size 16 \
  --trainer.max_epochs 40 --trainer.check_val_every_n_epoch 2 --trainer.accelerator gpu \
  --model.warmstart_ckpt base.ckpt      # fresh run; to resume, replace this line with --ckpt_path …/last.ckpt
```

Aim for
~10 k steps (steps = clips ÷ batch size × epochs). Set `max_epochs` so the learning rate decays over the run. Keep
checkpoints somewhere that survives a disconnect.

Measure before committing to a length. On a free Colab T4 (15 GB), batch 32 ran out of memory on the first step;
batch 16 fit and ran at 0.38 steps/s, so 120 epochs of 3,204 clips (21 k steps) would have taken ~16 h. 40 epochs
(~7 k steps, ~5 h) was the practical choice. Read the speed off the first epoch's progress bar, then set `max_epochs`.

### 6. Judge it by ear

Loss numbers don't tell you if it sounds right. Listen to the same fixed phrases at intervals: a long sentence, short
single words (flashcards are mostly single words), and the tricky cases for the language. Stop when it stops improving.

### 7. Export and plug in

Piper's exporter fails on torch ≥ 2.9 (gotcha 4), so run it through a 3-line `export.py` that forces the old one:

```python
import functools, runpy, torch
torch.onnx.export = functools.partial(torch.onnx.export, dynamo=False)
runpy.run_module("piper.train.export_onnx", run_name="__main__")
```

```bash
python export.py --checkpoint last.ckpt --output-file <lang>_<REGION>-<name>-medium.onnx
cp config.json <lang>_<REGION>-<name>-medium.onnx.json     # the config written during training
uv tool install piper-tts
mv <lang>_*.onnx* ~/.local/share/piper-voices/
echo "test" | piper -m ~/.local/share/piper-voices/<name>.onnx -f /tmp/t.wav && afplay /tmp/t.wav
```

Then set the deck profile's voice to `piper:<name>`. Voice ~5 cards with `add_audio`, listen in Anki, then do the rest.

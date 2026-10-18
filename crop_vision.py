"""AgriNova crop vision — on-device plant identification + water advice.

Model: Google AIY "plants" classifier (quantized MobileNet, ~5 MB,
2101 plant species, trained on iNaturalist). Runs fully offline.

Designed to be light enough for a Raspberry Pi 3B+:
  Mac   : /usr/bin/python3 -m pip install --user ai-edge-litert pillow numpy
  Pi 3B+: pip3 install tflite-runtime pillow numpy
Same file works on both — the TFLite runtime is auto-detected below.

Files expected next to this module:
  models/plants_V1.tflite
  models/plants_labels.csv
"""

import csv
import io
import os

import numpy as np
from PIL import Image

# --- TFLite runtime autodetect (Mac: ai-edge-litert; Pi: tflite-runtime) ---
Interpreter = None
try:
    from ai_edge_litert.interpreter import Interpreter        # Mac / x86
except ImportError:
    try:
        from tflite_runtime.interpreter import Interpreter    # Raspberry Pi
    except ImportError:
        try:
            from tensorflow.lite import Interpreter           # last resort
        except ImportError:
            pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, "models", "plants_V1.tflite")
LABELS_PATH = os.path.join(BASE_DIR, "models", "plants_labels.csv")
COMMON_PATH = os.path.join(BASE_DIR, "models", "plants_common.csv")  # offline, built once

# Common names + water profile for crops a farmer actually grows.
# Keyed by genus (first word of the scientific name, lowercase).
# need: how thirsty the crop is;  band: happy soil-moisture range (%).
CROP_INFO = {
    "solanum":      ("Tomato / Potato / Brinjal family", "high",   (60, 80)),
    "capsicum":     ("Chilli / Capsicum",                "medium", (50, 70)),
    "oryza":        ("Rice (Paddy)",                     "high",   (80, 100)),
    "triticum":     ("Wheat",                            "medium", (45, 65)),
    "zea":          ("Maize (Corn)",                     "medium", (50, 70)),
    "musa":         ("Banana",                           "high",   (65, 85)),
    "mangifera":    ("Mango",                            "low",    (35, 55)),
    "ocimum":       ("Tulsi / Basil",                    "medium", (50, 70)),
    "mentha":       ("Mint (Pudina)",                    "high",   (60, 80)),
    "coriandrum":   ("Coriander (Dhania)",               "medium", (50, 70)),
    "spinacia":     ("Spinach (Palak)",                  "high",   (60, 80)),
    "brassica":     ("Mustard / Cabbage family",         "medium", (50, 70)),
    "abelmoschus":  ("Okra (Bhindi)",                    "medium", (50, 70)),
    "cucumis":      ("Cucumber / Melon",                 "high",   (60, 80)),
    "citrus":       ("Lemon / Orange",                   "medium", (45, 65)),
    "psidium":      ("Guava",                            "low",    (40, 60)),
    "carica":       ("Papaya",                           "medium", (50, 70)),
    "allium":       ("Onion / Garlic",                   "low",    (40, 60)),
    "arachis":      ("Groundnut",                        "medium", (45, 65)),
    "gossypium":    ("Cotton",                           "low",    (35, 55)),
    "saccharum":    ("Sugarcane",                        "high",   (65, 85)),
    "vigna":        ("Moong / Beans",                    "low",    (40, 60)),
    "cicer":        ("Chickpea (Chana)",                 "low",    (35, 55)),
    "helianthus":   ("Sunflower",                        "medium", (45, 65)),
    "rosa":         ("Rose",                             "medium", (50, 70)),
    "hibiscus":     ("Hibiscus",                         "medium", (50, 70)),
    "epipremnum":   ("Money plant (Pothos)",             "low",    (40, 60)),
    "chlorophytum": ("Spider plant",                     "low",    (40, 60)),
    "aloe":         ("Aloe vera",                        "low",    (20, 40)),
    "ficus":        ("Ficus / Peepal family",            "medium", (45, 65)),
    "azadirachta":  ("Neem",                             "low",    (30, 50)),
    "curcuma":      ("Turmeric (Haldi)",                 "high",   (60, 80)),
    "zingiber":     ("Ginger (Adrak)",                   "high",   (60, 80)),
    "coleus":       ("Coleus",                           "medium", (50, 70)),
    "tagetes":      ("Marigold (Genda)",                 "medium", (45, 65)),
}
DEFAULT_BAND = (45, 70)  # unknown species: assume moderate watering

_interp = None
_labels = None
_common = None


def _common_names():
    global _common
    if _common is None:
        _common = {}
        try:
            with open(COMMON_PATH, newline="") as f:
                for row in csv.DictReader(f):
                    if row["common"]:
                        _common[row["scientific"]] = row["common"]
        except OSError:
            pass
    return _common


def display_name(scientific_name):
    """Best farmer-friendly name: our crop table > Wikipedia/GBIF > genus > Latin."""
    info = CROP_INFO.get(scientific_name.split()[0].lower())
    if info:
        return info[0]
    common = _common_names().get(scientific_name)
    if common:
        return common
    genus = scientific_name.split()[0]
    for sci, c in _common_names().items():
        if sci.split()[0] == genus:
            return f"{c} family"
    return scientific_name


def available():
    return Interpreter is not None and os.path.exists(MODEL_PATH)


def _load():
    global _interp, _labels
    if _interp is None:
        _interp = Interpreter(model_path=MODEL_PATH, num_threads=2)
        _interp.allocate_tensors()
    if _labels is None:
        _labels = {}
        with open(LABELS_PATH, newline="") as f:
            for row in csv.DictReader(f):
                _labels[int(row["id"])] = row["name"]
    return _interp, _labels


def identify(image_bytes, top_k=3):
    """JPEG/PNG bytes -> [(scientific_name, confidence 0..1), ...]"""
    interp, labels = _load()
    inp = interp.get_input_details()[0]
    out = interp.get_output_details()[0]
    h, w = inp["shape"][1], inp["shape"][2]

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    # centre-crop to square so a landscape webcam frame doesn't squash the plant
    s = min(img.size)
    left, top = (img.width - s) // 2, (img.height - s) // 2
    img = img.crop((left, top, left + s, top + s)).resize((w, h), Image.BILINEAR)

    arr = np.asarray(img, dtype=np.uint8)[None, ...]
    if inp["dtype"] == np.float32:
        arr = (arr.astype(np.float32) - 127.5) / 127.5
    interp.set_tensor(inp["index"], arr)
    interp.invoke()
    scores = interp.get_tensor(out["index"])[0]
    if scores.dtype == np.uint8:
        scale, zero = out["quantization"]
        scores = (scores.astype(np.float32) - zero) * scale

    order = np.argsort(scores)[::-1]
    results = []
    for i in order[:top_k + 1]:
        name = labels.get(int(i), f"class {i}")
        if name == "background":
            continue
        results.append((name, float(scores[i])))
    return results[:top_k]


def crop_info(scientific_name):
    """Scientific name -> (common_name, need, (band_lo, band_hi)) or None."""
    genus = scientific_name.split()[0].lower()
    return CROP_INFO.get(genus)


def water_advice(scientific_name, moisture, temp=None):
    """Compose farmer-friendly water advice from species + live soil reading."""
    info = crop_info(scientific_name)
    _, need, band = info if info else (None, "medium", DEFAULT_BAND)
    lo, hi = band
    name = display_name(scientific_name)
    lines = [f"🌱 Looks like: {name}",
             f"💧 Water need: {need} — happiest at {lo}–{hi}% soil moisture"]
    if moisture is None:
        lines.append("Soil sensor has no reading yet, so I can't compare.")
        return "\n".join(lines), None
    hot = temp is not None and temp >= 33
    if moisture < lo:
        gap = lo - moisture
        secs = max(5, min(30, int(gap * 0.8)))
        if hot:
            secs = min(30, secs + 5)
        lines.append(f"Your soil: {moisture}% — below its happy range. "
                     f"Suggest a ~{secs}s spray{' (it is hot today, a little extra)' if hot else ''}.")
        return "\n".join(lines), secs
    if moisture > hi:
        lines.append(f"Your soil: {moisture}% — wetter than this crop likes. "
                     "Skip watering and let it dry a bit.")
    else:
        lines.append(f"Your soil: {moisture}% — right in the happy range. No water needed. ✅")
    return "\n".join(lines), None


if __name__ == "__main__":
    import sys
    with open(sys.argv[1], "rb") as f:
        data = f.read()
    for name, conf in identify(data):
        print(f"{conf:5.1%}  {display_name(name)}  [{name}]  -> {crop_info(name)}")

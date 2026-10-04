"""Run inside the bot image to verify the real Ollama API and GPU after installation."""
import json
from pathlib import Path
from urllib.request import urlopen
from ai_review import review
from bot import config

c = config("/config/config.json")
a = dict(c["ai"], timeout_seconds=60)
observation = {"candle": 0, "observed_at": 0, "product": "BTC-EUR", "position_open": False,
               "cooldown_seconds": 900, "limits": c["ai_risk"],
               "note": "Installationstest. Ingen handel tilladt."}
result = review(a, observation)
if result["action"] != "WAIT":
    raise ValueError("Installationstest: modellen afviste ikke et køb under cooldown")
with urlopen(a["url"].rstrip("/") + "/api/ps", timeout=10) as r:
    loaded = json.load(r)
models = loaded.get("models", [])
match = [m for m in models if m.get("name", "").split(":")[0] == a["model"].split(":")[0]]
if not match or not any(m.get("size_vram", 0) > 0 for m in match):
    raise ValueError("Modellen blev ikke fundet på GPU. Kontrollér NVIDIA Driver, UUID og GPU-adgang.")
with urlopen(a["url"].rstrip("/") + "/api/tags", timeout=10) as r:
    tags = json.load(r)
manifest = {"configured_model": a["model"], "smoke_review": result, "loaded_models": loaded,
            "installed_models": tags}
Path("/data/ai-installation.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
print("Ollama JSON-svar OK; GPU i brug; model-digest gemt i /data/ai-installation.json")
print(result["reason"])

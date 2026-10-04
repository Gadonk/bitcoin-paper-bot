"""Persistent connection preferences; API keys never enter trading state or status JSON."""
import copy
import json
import os
from pathlib import Path
import secrets
import threading
from ai_review import validate_settings


class AISettings:
    def __init__(self, path, config):
        self.path = Path(path)
        self.config = config
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        defaults = {"enabled": os.environ.get("AI_ENABLED", "false").lower() == "true",
                    "url": config["ai"]["url"], "model": config["ai"]["model"],
                    "api_key": os.environ.get("AI_API_KEY", ""), "revision": 0}
        self.value = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else defaults
        self.validate(self.value)

    def validate(self, value):
        if type(value["enabled"]) is not bool or not isinstance(value["url"], str) or not isinstance(value["model"], str):
            raise ValueError("Ugyldige AI-indstillinger")
        if not isinstance(value["api_key"], str) or len(value["api_key"]) > 4096 or any(x in value["api_key"] for x in ("\r", "\n")):
            raise ValueError("Ugyldig API-nøgle")
        c = copy.deepcopy(self.config)
        c["ai"].update(url=value["url"], model=value["model"])
        validate_settings(c)

    def public(self):
        with self.lock:
            return {k: self.value[k] for k in ("enabled", "url", "model", "revision")} | {"has_api_key": bool(self.value["api_key"]), "token": self.token}

    def update(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"enabled", "url", "model", "api_key", "remove_api_key"}:
            raise ValueError("Ukendte AI-indstillinger")
        with self.lock:
            v = copy.deepcopy(self.value)
            for key in ("enabled", "url", "model"):
                if key in payload:
                    v[key] = payload[key].strip() if isinstance(payload[key], str) else payload[key]
            if not v["model"]:
                v["model"] = "qwen3.5:4b"
            if payload.get("remove_api_key") is True:
                v["api_key"] = ""
            elif payload.get("api_key"):
                v["api_key"] = payload["api_key"]
            self.validate(v)
            v["revision"] += 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(v, f, ensure_ascii=False)
            os.replace(temp, self.path)
            self.value = v
            return self.public()

    def apply(self, paper):
        with self.lock:
            if getattr(paper, "ai_preferences_revision", None) != self.value["revision"]:
                paper.ai_enabled = self.value["enabled"]
                paper.ai_runtime = {**paper.c["ai"], "url": self.value["url"], "model": self.value["model"], "api_key": self.value["api_key"]}
                from ai_review import Reviewer
                paper.reviewer = Reviewer(paper.ai_runtime)
                paper.ai_preferences_revision = self.value["revision"]

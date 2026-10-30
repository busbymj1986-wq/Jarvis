"""Check an event snapshot with Claude before raising an alarm.

Most NVR triggers at night are shadows, headlights, insects on the lens or
animals. A vision check on the snapshot filters those out and writes a short
description for the alert and the report.
"""
from __future__ import annotations

import base64
import json
import logging
import os

import anthropic

log = logging.getLogger("vigi.ai")

MAX_IMAGE_BYTES = 5 * 1024 * 1024

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "people": {"type": "integer", "description": "Number of people visible"},
        "vehicles": {"type": "integer", "description": "Number of vehicles visible"},
        "animals": {"type": "integer", "description": "Number of animals visible"},
        "description": {
            "type": "string",
            "description": "One or two short factual sentences for a phone alert",
        },
        "threat_level": {"type": "string", "enum": ["none", "low", "medium", "high"]},
        "alarm": {"type": "boolean", "description": "Whether a person should be alerted now"},
        "reason": {"type": "string", "description": "Brief justification for the alarm decision"},
    },
    "required": ["people", "vehicles", "animals", "description", "threat_level", "alarm", "reason"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You verify CCTV alarm triggers for a property security system. \
A camera fired a detection event while the system was armed; you see the snapshot \
taken at that moment and decide whether the owner or a response team should be alerted.

Alert when a person is present in the scene, or a vehicle is somewhere it should not \
be while the property is armed. Treat these as more serious: climbing walls or gates, \
handling locks, doors or windows, carrying tools or bags away, covered faces, or \
several people moving together.

Do not alert when nobody is there: shadows, headlights or sunlight changes, rain, \
insects or spider webs on the lens, moving plants, animals on their own, or IR glare.

If the image is too dark, blurry or obstructed to tell whether a person is present, \
alert and say the image was unclear: a missed intrusion costs far more than a false alert.

Write the description as plain facts about what is visible and where in the frame \
(for example "One person in dark clothing at the side gate, facing the house"). \
Describe clothing and actions; do not try to identify anyone."""


class AnalysisError(Exception):
    pass


def ai_enabled(cfg: dict) -> bool:
    setting = cfg["ai"].get("enabled", "auto")
    if setting == "auto":
        return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    return bool(setting)


def _media_type(image: bytes) -> str:
    if image.startswith(b"\x89PNG"):
        return "image/png"
    return "image/jpeg"


class SnapshotAnalyzer:
    def __init__(self, cfg: dict, client: anthropic.Anthropic | None = None):
        self.cfg = cfg["ai"]
        self.enabled = ai_enabled(cfg)
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic(timeout=60.0, max_retries=2)
        return self._client

    def system_prompt(self) -> str:
        notes = (self.cfg.get("site_notes") or "").strip()
        if notes:
            return f"{SYSTEM_PROMPT}\n\nWhat is normal at this site:\n{notes}"
        return SYSTEM_PROMPT

    def analyze(self, image: bytes, context: dict) -> dict:
        if len(image) > MAX_IMAGE_BYTES:
            raise AnalysisError("snapshot larger than 5 MB; use the sub stream for snapshots")

        details = "\n".join(f"{k}: {v}" for k, v in context.items() if v not in (None, ""))
        try:
            response = self.client.beta.messages.create(
                model=self.cfg.get("model", "claude-opus-5-5"),
                max_tokens=4096,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={
                    "effort": self.cfg.get("effort", "low"),
                    "format": {"type": "json_schema", "schema": VERDICT_SCHEMA},
                },
                system=self.system_prompt(),
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": _media_type(image),
                                "data": base64.standard_b64encode(image).decode("ascii"),
                            },
                        },
                        {"type": "text", "text": f"Detection event:\n{details}"},
                    ],
                }],
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise AnalysisError(f"Claude API credentials rejected: {exc.message}") from exc
        except anthropic.BadRequestError as exc:
            raise AnalysisError(f"Claude API rejected the request: {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise AnalysisError("Claude API rate limit reached") from exc
        except anthropic.APIStatusError as exc:
            raise AnalysisError(f"Claude API error {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise AnalysisError("could not reach the Claude API") from exc

        if response.stop_reason == "refusal":
            raise AnalysisError("Claude declined to analyse this snapshot")
        if response.stop_reason == "max_tokens":
            raise AnalysisError("Claude response was cut off")

        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            verdict = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AnalysisError("Claude returned invalid JSON") from exc
        verdict["model"] = response.model
        return verdict

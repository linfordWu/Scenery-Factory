# SPDX-License-Identifier: GPL-3.0-only
"""Screenwriter agent: brief/style/duration -> title, script, scene list."""
from __future__ import annotations

from typing import Optional

from ..domain.schemas.core import Project, Scene, new_id

_SCHEMA_HINT = '{"title": str, "script": str, "scenes": [{"title": str, "summary": str}]}'


class ScreenwriterAgent:
    def __init__(self, text_model, store=None):
        self.text_model = text_model
        self.store = store

    def write(self, brief: str, style: str = "", duration_s: int = 60,
              project_id: Optional[str] = None) -> dict:
        instructions = (
            "You are the screenwriter of a short-drama factory. Write a compact "
            "vertical short-drama script in Chinese from the brief. Split it into "
            "scenes; each scene gets a short title and a one-sentence summary. "
            f"Total runtime target: {duration_s} seconds."
        )
        out = self.text_model.chat_json(
            instructions,
            {"brief": brief, "style": style, "duration_s": duration_s},
            _SCHEMA_HINT)
        result = {
            "title": str(out.get("title", "")),
            "script": str(out.get("script", "")),
            "scenes": [
                {"title": str(s.get("title", "")), "summary": str(s.get("summary", ""))}
                for s in out.get("scenes") or []
            ],
        }
        if self.store is not None and project_id:
            for i, s in enumerate(result["scenes"]):
                self.store.put("scenes", Scene(
                    scene_id=new_id("sc"), project_id=project_id, order=i,
                    title=s["title"], summary=s["summary"]))
        return result

    def write_project(self, project: Project) -> dict:
        return self.write(project.brief, project.style,
                          project.duration_target_s, project.project_id)

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PromptRef:
    name: str
    path: Path


class PromptManager:
    """File-based prompt loader with simple {{VAR}} substitution.

    Expected config shape (in pipeline.yaml):

    prompts:
      dir: prompts
      profile: china_zh
      profiles:
        china_zh:
          scoring: zh/scoring_v1.txt
          story: zh/story_v1.txt
          script: zh/script_v1.txt

    You can switch profile to swap the entire prompt suite.
    """

    def __init__(self, cfg: dict[str, Any] | None, *, project_root: str | Path | None = None):
        self.cfg = cfg or {}
        self.project_root = Path(project_root) if project_root else Path(".")
        self._cache: dict[str, str] = {}

    def _resolve_prompt_path(self, name: str) -> PromptRef:
        prompts_dir = Path(self.cfg.get("dir", "prompts"))
        profile = self.cfg.get("profile") or "default"
        profiles = self.cfg.get("profiles") or {}

        prof = profiles.get(profile)
        if not prof:
            raise KeyError(f"prompts.profiles missing profile={profile!r}")
        rel = prof.get(name)
        if not rel:
            raise KeyError(f"prompts.profiles[{profile!r}] missing prompt key={name!r}")

        path = (self.project_root / prompts_dir / Path(str(rel))).resolve()
        if not path.is_file() and prompts_dir == Path('prompts'):
            # Built-in prompts ship inside the package. A working-directory
            # prompts/ tree remains a supported user override.
            path = Path(__file__).parent / str(rel)
        return PromptRef(name=name, path=path)

    def get(self, name: str) -> str:
        ref = self._resolve_prompt_path(name)
        key = str(ref.path)
        if key in self._cache:
            return self._cache[key]
        txt = ref.path.read_text(encoding="utf-8")
        self._cache[key] = txt
        return txt

    @staticmethod
    def render(template: str, variables: dict[str, Any]) -> str:
        """Very small templating helper.

        We intentionally keep this minimal (and deterministic) to avoid
        pulling in a second templating engine besides Jinja (already used
        for export). Prompts only need string substitution.
        """
        out = template
        for k, v in (variables or {}).items():
            out = out.replace("{{" + k + "}}", str(v))
        return out

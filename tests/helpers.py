"""Test helpers."""
from typing import Callable, List, Optional

from backend.llm.client import TranslationLLMClient


class FakeLLMClient(TranslationLLMClient):
    """Real client logic, fake transport. Records every call.

    handler(system_prompt, user_prompt, model_override) -> str  (may raise)
    Default handler: translates each `[[#i]]` block to `VI(<text>)`, or the whole text if no markers.
    """

    def __init__(self, config=None, cache=None, handler: Optional[Callable] = None, **kw):
        super().__init__(config=config, cache=cache, **kw)
        self.calls: List[tuple] = []
        self.handler = handler or self.default_handler

    @staticmethod
    def default_handler(system_prompt, user_prompt, model_override=None):
        import re

        parts = re.split(r"(\[\[#\d+\]\])", user_prompt)
        if len(parts) > 1:
            out = []
            for i in range(1, len(parts), 2):
                out.append(f"{parts[i]}\nVI({parts[i + 1].strip()})")
            return "\n\n".join(out)
        return f"VI({user_prompt.strip()})"

    async def _call_llm_with_rotation(self, system_prompt, user_prompt, model_override=None):
        self.calls.append((system_prompt, user_prompt, model_override))
        res = self.handler(system_prompt, user_prompt, model_override)
        return res

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Union
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field, field_validator

from .cache import TranslationCache, default_cache
from .glossary import (
    extract_candidate_terms_heuristic,
    filter_glossary_for_text,
    normalize_glossary,
    parse_llm_glossary_json,
)
from .prompts import (
    build_batch_user_prompt,
    build_editor_batch_user_prompt,
    build_editor_system_prompt,
    build_glossary_extraction_prompt,
    build_system_prompt,
    resolve_genre_prompt,
    resolve_lang_code,
)

logger = logging.getLogger(__name__)


DEFAULT_BASE_URLS: Dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://localhost:11434/v1",
    "claude": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
}

GEMINI_SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]


class LLMConfig(BaseModel):
    provider: str = Field(
        default="openai",
        description="Supported: google_free, gemini, openai, deepseek, claude, openrouter, ollama, hybrid",
    )
    api_keys: List[str] = Field(
        default_factory=lambda: [os.environ["BTH_API_KEY"]] if os.environ.get("BTH_API_KEY") else []
    )
    model: str = Field(default="cc/claude-sonnet-5-5")
    base_url: Optional[str] = Field(default="https://ryg2i56.abc-tunnel.us/v1")
    gemini_api_keys: List[str] = Field(default_factory=list)
    gemini_model: str = Field(default="gemini-2.5-pro")
    gemini_base_url: Optional[str] = Field(default=None)
    target_lang: str = Field(default="Tiếng Việt")
    target_lang_code: str = Field(default="vi")
    concurrency: int = Field(default=12, ge=1, le=32)
    bilingual: bool = Field(default=False)
    auto_glossary: bool = Field(default=True)
    enable_editor_agent: bool = Field(default=True)
    agent_glossary_model: Optional[str] = Field(default=None)
    agent_translator_model: Optional[str] = Field(default=None)
    agent_editor_model: Optional[str] = Field(default="cc/claude-sonnet-5-5")
    genre_preset: str = Field(default="auto")
    custom_prompt: str = Field(default="")
    chapter_limit: int = Field(default=0, ge=0)
    page_range: str = Field(default="")
    pause_for_glossary: bool = Field(default=False)
    custom_glossary: List[Dict[str, str]] = Field(default_factory=list)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    fallback_to_google_free: bool = Field(default=True)

    @field_validator("api_keys", "gemini_api_keys", mode="before")
    @classmethod
    def parse_api_keys(cls, v: Union[str, List[str], None]) -> List[str]:
        if not v:
            return []
        if isinstance(v, str):
            raw_parts = re.split(r"[,;\r\n]+", v)
            return [k.strip() for k in raw_parts if k and k.strip()]
        if isinstance(v, list):
            result: List[str] = []
            for item in v:
                if isinstance(item, str):
                    for sub in re.split(r"[,;\r\n]+", item):
                        if sub and sub.strip():
                            result.append(sub.strip())
            return result
        return []

    @field_validator("provider", mode="before")
    @classmethod
    def normalize_provider(cls, v: Any) -> str:
        if not v or not isinstance(v, str):
            return "google_free"
        return v.strip().lower()

    def model_post_init(self, __context: Any) -> None:
        if not self.target_lang_code or self.target_lang_code == "vi":
            self.target_lang_code = resolve_lang_code(
                self.target_lang, fallback=self.target_lang_code or "vi"
            )


class TranslationLLMClient:
    """
    High-concurrency Multi-Key & Hybrid Multi-Provider LLM Translation Engine.
    Supports Custom Claude Gateway, Google Gemini Ultra / AI Studio, Hybrid Load-Balancing,
    OpenAI / DeepSeek / OpenRouter / Ollama, and zero-key Google/Bing Free mode.
    """

    def __init__(
        self,
        config: Optional[LLMConfig] = None,
        cache: Optional[TranslationCache] = None,
        agent_event_cb: Optional[Any] = None,
    ):
        self.config = config or LLMConfig()
        self.cache = cache or default_cache
        self.semaphore = asyncio.Semaphore(self.config.concurrency)
        self._key_index = 0
        self._gemini_key_index = 0
        self._hybrid_counter = 0
        self._key_lock = asyncio.Lock()
        self.agent_event_cb = agent_event_cb
        self.last_editor_diffs: Dict[str, Dict[str, str]] = {}

    async def _emit_agent_event(self, agent_id: int, agent_name: str, action: str, detail: str = "", model: str = "") -> None:
        if self.agent_event_cb:
            try:
                res = self.agent_event_cb({
                    "agent_id": agent_id,
                    "agent_name": agent_name,
                    "action": action,
                    "detail": detail,
                    "model": model,
                })
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass

    async def _get_next_api_key(self, for_gemini: bool = False) -> str:
        if for_gemini:
            keys = self.config.gemini_api_keys or [
                k for k in self.config.api_keys if k.startswith("AIza")
            ] or self.config.api_keys
            if not keys:
                return ""
            async with self._key_lock:
                key = keys[self._gemini_key_index % len(keys)]
                self._gemini_key_index = (self._gemini_key_index + 1) % len(keys)
                return key

        keys = self.config.api_keys or self.config.gemini_api_keys
        if not keys:
            return ""
        async with self._key_lock:
            key = keys[self._key_index % len(keys)]
            self._key_index = (self._key_index + 1) % len(keys)
            return key

    def _effective_provider(self, model_override: Optional[str] = None) -> str:
        p = self.config.provider
        m = (model_override or self.config.model or "").lower()
        if m == "gtx-free":
            return "google_free"
        if m.startswith("hybrid"):
            has_gw = bool(self.config.api_keys)
            has_gm = bool(self.config.gemini_api_keys)
            if has_gw and has_gm:
                return "hybrid"
            return "gemini" if has_gm else ("openai" if has_gw else "google_free")
        if m.startswith("gemini") and p != "openrouter":
            if self.config.gemini_api_keys or any(
                k.startswith("AIza") for k in self.config.api_keys
            ):
                return "gemini"
        if model_override and (m.startswith("cc/") or m.startswith("claude") or m.startswith("gpt-") or m.startswith("deepseek")):
            if self.config.api_keys:
                return "openai"
        if p == "hybrid":
            has_gw = bool(self.config.api_keys)
            has_gm = bool(self.config.gemini_api_keys)
            if has_gw and has_gm:
                return "hybrid"
            if has_gm:
                return "gemini"
            if has_gw:
                return "openai"
            return "google_free"
        if p == "gemini":
            if self.config.gemini_api_keys or self.config.api_keys:
                return "gemini"
            return "google_free"
        if p not in ("google_free", "ollama") and not (
            self.config.api_keys or self.config.gemini_api_keys
        ):
            return "google_free"
        return p

    def _effective_model(self, model_override: Optional[str] = None) -> str:
        provider = self._effective_provider(model_override=model_override)
        if provider == "google_free":
            return "gtx-free"
        if provider == "hybrid":
            return f"{self.config.model} + {self.config.gemini_model}"
        chosen = model_override or self.config.model
        if provider == "gemini" and not (chosen or "").lower().startswith("gemini"):
            return self.config.gemini_model or "gemini-2.5-pro"
        return chosen or "cc/claude-sonnet-5-5"

    async def _translate_google_free_raw(
        self, text: str, target_lang_code: Optional[str] = None
    ) -> str:
        """
        Calls Google Translate's free GTX endpoint (`translate_a/single`).
        Handles long texts by chunking cleanly along paragraph/sentence boundaries if needed.
        """
        if not text or not text.strip():
            return text

        tl = target_lang_code or resolve_lang_code(
            self.config.target_lang, self.config.target_lang_code or "vi"
        )

        # If text is longer than ~4200 chars, split by double newline to keep URL/payload safe
        if len(text) > 4200 and "\n\n" in text:
            paragraphs = text.split("\n\n")
            chunks: List[str] = []
            current_chunk: List[str] = []
            current_len = 0
            for p in paragraphs:
                if current_len + len(p) > 3800 and current_chunk:
                    chunks.append("\n\n".join(current_chunk))
                    current_chunk = [p]
                    current_len = len(p)
                else:
                    current_chunk.append(p)
                    current_len += len(p) + 2
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))

            translated_chunks = await asyncio.gather(
                *[self._translate_google_free_single(c, tl) for c in chunks]
            )
            return "\n\n".join(translated_chunks)

        return await self._translate_google_free_single(text, tl)

    async def _translate_google_free_single(self, text: str, tl: str) -> str:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            )
        }

        endpoints = [
            (
                "https://translate.googleapis.com/translate_a/single",
                {"client": "dict-chrome-ex", "sl": "auto", "tl": tl, "dt": "t"},
                "single",
            ),
            (
                "https://clients5.google.com/translate_a/t",
                {"client": "dict-chrome-ex", "sl": "auto", "tl": tl},
                "clients5",
            ),
            (
                "https://translate.googleapis.com/translate_a/single",
                {"client": "gtx", "sl": "auto", "tl": tl, "dt": "t"},
                "single",
            ),
        ]

        last_err: Optional[Exception] = None
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            for url, base_params, mode in endpoints:
                try:
                    if len(text) > 1000:
                        resp = await client.post(
                            url,
                            params=base_params,
                            data={"q": text},
                            headers=headers,
                        )
                    else:
                        get_params = {**base_params, "q": text}
                        resp = await client.get(url, params=get_params, headers=headers)

                    if resp.status_code == 429 or resp.status_code >= 500:
                        last_err = RuntimeError(f"HTTP {resp.status_code} from {url}")
                        continue

                    resp.raise_for_status()
                    data = resp.json()

                    if mode == "single":
                        if (
                            isinstance(data, list)
                            and len(data) > 0
                            and isinstance(data[0], list)
                        ):
                            segments = [
                                seg[0]
                                for seg in data[0]
                                if isinstance(seg, list) and len(seg) > 0 and seg[0]
                            ]
                            if segments:
                                return "".join(segments).strip()
                    elif mode == "clients5":
                        # Format: [["Translated text", "en"]] or ["Translated text"]
                        if isinstance(data, list) and len(data) > 0:
                            first = data[0]
                            if isinstance(first, list) and len(first) > 0 and isinstance(first[0], str):
                                return first[0].strip()
                            if isinstance(first, str):
                                return first.strip()
                except Exception as exc:
                    last_err = exc
                    continue

        # Fallback to Bing Neural Translator (100% free, no API key needed, immune to Google 429)
        try:
            bing_out = await self._translate_bing_free_single(text, tl)
            if bing_out and bing_out.strip():
                return bing_out.strip()
        except Exception as bing_exc:
            last_err = bing_exc

        logger.warning("Free translation failed after retries: %s", last_err)
        return text

    async def _translate_bing_free_single(self, text: str, tl: str) -> str:
        bing_lang_map = {"zh": "zh-Hans", "zh-cn": "zh-Hans", "zh-tw": "zh-Hant"}
        to_lang = bing_lang_map.get(tl.lower(), tl)
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
        )
        async with httpx.AsyncClient(
            headers={"User-Agent": ua}, timeout=20.0, follow_redirects=True
        ) as client:
            r_init = await client.get("https://www.bing.com/translator")
            r_init.raise_for_status()
            html_body = r_init.text
            base_url = str(r_init.url)
            if base_url.endswith("translator"):
                base_url = base_url[:-10]
            ig_match = re.findall(r'"ig":"(.*?)"', html_body)
            iid_match = re.findall(r'data-iid="(.*?)"', html_body)
            abuse_match = re.findall(
                r'params_AbusePreventionHelper\s*=\s*\[(.*?),"(.*?)",', html_body
            )
            if not (ig_match and iid_match and abuse_match):
                raise RuntimeError("Could not parse Bing Translator session tokens")
            ig = ig_match[0]
            iid = iid_match[-1]
            key, token = abuse_match[0]

            r_tr = await client.post(
                f"{base_url}ttranslatev3?IG={ig}&IID={iid}&isVertical=1",
                data={
                    "fromLang": "auto-detect",
                    "to": to_lang,
                    "text": text[:1000],
                    "token": token,
                    "key": key,
                },
            )
            r_tr.raise_for_status()
            payload = r_tr.json()
            if isinstance(payload, list) and len(payload) > 0:
                translations = payload[0].get("translations") or []
                if translations and translations[0].get("text"):
                    return translations[0]["text"]
            raise RuntimeError(f"Unexpected Bing response: {str(payload)[:120]}")

    async def _call_gemini(
        self,
        system_prompt: str,
        user_prompt: str,
        api_key: str,
        model_override: Optional[str] = None,
    ) -> str:
        raw_model = (
            model_override
            or (
                self.config.model
                if (self.config.model or "").lower().startswith("gemini")
                else self.config.gemini_model
            )
            or "gemini-2.5-pro"
        ).strip()
        if raw_model.startswith("models/"):
            raw_model = raw_model[len("models/") :]

        gemini_base = (
            self.config.gemini_base_url or DEFAULT_BASE_URLS["gemini"]
        ).rstrip("/")

        # If user's Gemini Ultra key is an OpenAI-compatible gateway key (e.g. sk-...) or custom OpenAI proxy URL
        if (
            api_key.startswith("sk-")
            or (
                self.config.gemini_base_url
                and "googleapis.com" not in gemini_base.lower()
            )
        ):
            proxy_base = (
                self.config.gemini_base_url
                or self.config.base_url
                or DEFAULT_BASE_URLS["openai"]
            ).rstrip("/")
            return await self._call_openai_compatible(
                system_prompt,
                user_prompt,
                api_key,
                model_override=raw_model,
                base_url_override=proxy_base,
            )

        # Native Google AI Studio / Gemini Ultra REST API (:generateContent)
        # Map alias 'gemini-ultra' -> 'gemini-2.5-pro' on native googleapis endpoint if needed
        api_model = "gemini-2.5-pro" if raw_model.lower() == "gemini-ultra" else raw_model
        url = f"{gemini_base}/models/{quote(api_model)}:generateContent?key={api_key}"

        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {
                "temperature": self.config.temperature,
            },
            "safetySettings": GEMINI_SAFETY_SETTINGS,
        }

        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()

        candidates = data.get("candidates") or []
        if not candidates:
            raise ValueError(f"Gemini returned no candidates: {data}")
        content = candidates[0].get("content") or {}
        parts = content.get("parts") or []
        texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
        result = "".join(texts).strip()
        if not result:
            raise ValueError("Gemini returned empty text response.")
        return result

    async def _call_openai_compatible(
        self,
        system_prompt: str,
        user_prompt: str,
        api_key: str,
        model_override: Optional[str] = None,
        base_url_override: Optional[str] = None,
    ) -> str:
        provider = self.config.provider
        base = (
            base_url_override
            or self.config.base_url
            or DEFAULT_BASE_URLS.get(provider, DEFAULT_BASE_URLS["openai"])
        ).rstrip("/")
        url = f"{base}/chat/completions"

        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if provider == "openrouter":
            headers["HTTP-Referer"] = "https://booktranslatorhub.local"
            headers["X-Title"] = "BookTranslatorHub"

        model_name = model_override or self.config.model or "cc/claude-sonnet-5-5"
        payload: Dict[str, Any] = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
        }
        # Claude adaptive thinking models on OpenAI-compatible proxies reject non-1.0 temperature
        if "claude" not in model_name.lower() and "cc/" not in model_name.lower():
            payload["temperature"] = self.config.temperature

        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        choices = data.get("choices") or []
        if not choices:
            raise ValueError(f"OpenAI-compatible API returned no choices: {data}")
        message = choices[0].get("message") or {}
        content = (message.get("content") or "").strip()
        if not content:
            raise ValueError("OpenAI-compatible API returned empty content.")
        return content

    async def _call_claude(
        self, system_prompt: str, user_prompt: str, api_key: str
    ) -> str:
        base = (self.config.base_url or DEFAULT_BASE_URLS["claude"]).rstrip("/")
        # If using an OpenAI-compatible tunnel/gateway with Claude selected, route to /chat/completions
        if "anthropic.com" not in base.lower():
            return await self._call_openai_compatible(
                system_prompt, user_prompt, api_key
            )
        url = f"{base}/messages"

        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": self.config.model or "claude-3-5-sonnet-latest",
            "max_tokens": 8192,
            "temperature": self.config.temperature,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }

        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        content_blocks = data.get("content") or []
        texts = [
            b.get("text", "")
            for b in content_blocks
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        result = "".join(texts).strip()
        if not result:
            raise ValueError("Claude API returned empty content.")
        return result

    async def _call_llm_with_rotation(
        self,
        system_prompt: str,
        user_prompt: str,
        model_override: Optional[str] = None,
    ) -> str:
        """
        Executes an LLM request using the Multi-Key Rotation Pool and Exponential Backoff
        when encountering HTTP 429 (Rate Limit) or 5xx transient server errors.
        Supports 'hybrid' mode which load-balances across both Gateway (Claude/OpenAI) and Gemini Ultra pools,
        as well as per-Agent model_override routing.
        """
        provider = self._effective_provider(model_override=model_override)
        if provider == "google_free":
            return await self._translate_google_free_raw(user_prompt)

        total_keys = max(
            1, len(self.config.api_keys) + len(self.config.gemini_api_keys)
        )
        max_attempts = max(3, min(total_keys * 2, 8))
        last_error: Optional[Exception] = None

        for attempt in range(max_attempts):
            active_sub_provider = provider
            if provider == "hybrid":
                async with self._key_lock:
                    use_gemini = (self._hybrid_counter % 2) == 1
                    self._hybrid_counter += 1
                active_sub_provider = "gemini" if use_gemini else "openai"

            is_gemini_call = active_sub_provider == "gemini"
            api_key = await self._get_next_api_key(for_gemini=is_gemini_call)

            try:
                if is_gemini_call:
                    gm_override = (
                        model_override
                        if (model_override and model_override.lower().startswith("gemini"))
                        else (self.config.gemini_model if provider == "hybrid" else None)
                    )
                    return await self._call_gemini(
                        system_prompt,
                        user_prompt,
                        api_key,
                        model_override=gm_override,
                    )
                elif active_sub_provider in (
                    "openai",
                    "deepseek",
                    "openrouter",
                    "ollama",
                ):
                    gw_override = (
                        model_override
                        if (model_override and not model_override.lower().startswith("hybrid"))
                        else None
                    )
                    return await self._call_openai_compatible(
                        system_prompt,
                        user_prompt,
                        api_key,
                        model_override=gw_override,
                    )
                elif active_sub_provider == "claude":
                    return await self._call_claude(system_prompt, user_prompt, api_key)
                else:
                    return await self._call_openai_compatible(
                        system_prompt,
                        user_prompt,
                        api_key,
                        model_override=model_override,
                    )
            except httpx.HTTPStatusError as http_err:
                last_error = http_err
                status = http_err.response.status_code
                if status in (429, 500, 502, 503, 504):
                    backoff = min(12.0, 1.0 * (2 ** (attempt // max(1, total_keys))))
                    logger.warning(
                        "LLM HTTP %s on provider=%s (attempt %d/%d). Rotating key & backing off %.1fs...",
                        status,
                        active_sub_provider,
                        attempt + 1,
                        max_attempts,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
                elif status in (401, 403) and (total_keys > 1 or provider == "hybrid"):
                    logger.warning(
                        "Auth error HTTP %s on one key; rotating to next key...", status
                    )
                    continue
                else:
                    if provider == "hybrid":
                        continue
                    break
            except Exception as exc:
                last_error = exc
                backoff = min(6.0, 0.8 * (2**attempt))
                logger.warning(
                    "LLM error on provider=%s (attempt %d/%d): %s",
                    active_sub_provider,
                    attempt + 1,
                    max_attempts,
                    exc,
                )
                await asyncio.sleep(backoff)

        raise RuntimeError(
            f"All LLM attempts failed for provider '{provider}': {last_error}"
        )

    @staticmethod
    def _apply_glossary_post_replacement(
        translated_text: str,
        src_text: str,
        matched_glossary: List[Dict[str, str]],
    ) -> str:
        """
        For `google_free` mode, ensures any explicit user glossary mappings where `src != dst`
        that remained untranslated in the output are replaced accurately.
        """
        if not matched_glossary or not translated_text:
            return translated_text
        output = translated_text
        for item in matched_glossary:
            src = item.get("src", "").strip()
            dst = item.get("dst", "").strip()
            if src and dst and src.lower() != dst.lower():
                pattern = re.compile(re.escape(src), re.IGNORECASE)
                if pattern.search(output):
                    output = pattern.sub(dst, output)
        return output

    async def translate_text(
        self,
        text: str,
        context: str = "",
        glossary: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        """
        Translates a single paragraph or text segment using Agent 3 (Translator)
        and optionally polishes with Agent 4 (Chief Editor) if enabled.
        """
        if not text or not text.strip():
            return text

        tr_model_override = self.config.agent_translator_model or None
        provider = self._effective_provider(model_override=tr_model_override)
        model = self._effective_model(model_override=tr_model_override)
        ed_tag = f"+ed:{self.config.agent_editor_model or model}" if self.config.enable_editor_agent else ""
        cache_model_key = f"{model}{ed_tag}"
        target_lang = self.config.target_lang

        # 1. Check SQLite Cache
        cached = self.cache.get(text, target_lang, provider, cache_model_key)
        if cached:
            return cached

        matched_glossary = filter_glossary_for_text(text, glossary)

        async with self.semaphore:
            try:
                if provider == "google_free":
                    translated = await self._translate_google_free_raw(text)
                    translated = self._apply_glossary_post_replacement(
                        translated, text, matched_glossary
                    )
                else:
                    effective_prompt = resolve_genre_prompt(
                        self.config.genre_preset, self.config.custom_prompt
                    )
                    sys_prompt = build_system_prompt(
                        target_lang=target_lang,
                        custom_prompt=effective_prompt,
                        glossary=matched_glossary,
                        context=context,
                        is_batch=False,
                    )
                    translated = await self._call_llm_with_rotation(
                        sys_prompt, text, model_override=tr_model_override
                    )
                    translated = re.sub(r"^\[\[#\d+\]\]\s*", "", translated).strip()
                    if self.config.enable_editor_agent and translated:
                        draft_copy = translated
                        polished_single = await self.polish_single_segment(
                            src_text=text,
                            draft_text=draft_copy,
                            glossary=matched_glossary,
                        )
                        if polished_single and polished_single.strip():
                            translated = polished_single.strip()
                            self.last_editor_diffs[text.strip()] = {
                                "draft": draft_copy,
                                "polished": translated,
                                "editor_model": self._effective_model(
                                    model_override=self.config.agent_editor_model or self.config.model
                                ),
                            }
            except Exception as exc:
                logger.error("translate_text failed with LLM (%s), falling back to google_free", exc)
                if self.config.fallback_to_google_free:
                    translated = await self._translate_google_free_raw(text)
                    translated = self._apply_glossary_post_replacement(
                        translated, text, matched_glossary
                    )
                    provider = "google_free"
                    cache_model_key = "gtx-free"
                else:
                    raise

        if translated and translated.strip():
            self.cache.set(text, translated, target_lang, provider, cache_model_key)

        return translated

    @staticmethod
    def _parse_batch_response(raw_output: str, expected_count: int) -> Optional[List[str]]:
        """
        Parses LLM or Google Free batch output formatted with `[[#0]]`, `[[#1]]`, ... markers.
        Returns None if any index from `0..expected_count-1` is missing or empty.
        """
        if not raw_output or expected_count <= 0:
            return None

        # Match [[#0]], [[ #0 ]], [#0], etc. (tolerant of minor spacing variations)
        pattern = re.compile(r"\[\[\s*#\s*(\d+)\s*\]\]")
        matches = list(pattern.finditer(raw_output))
        if len(matches) != expected_count:
            return None

        extracted: Dict[int, str] = {}
        for i, match in enumerate(matches):
            idx = int(match.group(1))
            start_pos = match.end()
            end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(raw_output)
            segment_text = raw_output[start_pos:end_pos].strip()
            if not segment_text:
                return None
            extracted[idx] = segment_text

        result: List[str] = []
        for idx in range(expected_count):
            if idx not in extracted:
                return None
            result.append(extracted[idx])
        return result

    async def refine_batch_with_editor(
        self,
        sources: List[str],
        drafts: List[str],
        matched_glossary: Optional[List[Dict[str, str]]] = None,
    ) -> List[str]:
        """
        Agent 4: Chief Editor & Literary Critic Agent.
        Compares [SOURCE] and [DRAFT] segments and polishes awkward/literal phrasing into
        publication-grade prose while preserving 100% of numbers, formulas, and [[#i]] markers.
        """
        if not self.config.enable_editor_agent or not sources or len(sources) != len(drafts):
            return drafts

        ed_override = self.config.agent_editor_model or self.config.model
        ed_provider = self._effective_provider(model_override=ed_override)
        ed_model = self._effective_model(model_override=ed_override)
        if ed_provider == "google_free":
            return drafts

        try:
            effective_prompt = resolve_genre_prompt(
                self.config.genre_preset, self.config.custom_prompt
            )
            sys_prompt = build_editor_system_prompt(
                target_lang=self.config.target_lang,
                custom_prompt=effective_prompt,
                glossary=matched_glossary,
                is_batch=True,
            )
            user_prompt = build_editor_batch_user_prompt(sources, drafts)
            raw_edited = await self._call_llm_with_rotation(
                sys_prompt, user_prompt, model_override=ed_override
            )
            parsed_edited = self._parse_batch_response(raw_edited, len(sources))
            if parsed_edited and len(parsed_edited) == len(sources):
                refined_count = 0
                for src, dft, pol in zip(sources, drafts, parsed_edited):
                    if dft.strip() != pol.strip():
                        refined_count += 1
                    self.last_editor_diffs[src.strip()] = {
                        "draft": dft.strip(),
                        "polished": pol.strip(),
                        "editor_model": ed_model,
                    }
                await self._emit_agent_event(
                    agent_id=4,
                    agent_name="Agent 4 · Tổng Biên Tập",
                    action="refined_batch",
                    detail=f"Đã đối chiếu {len(sources)} đoạn & chuốt lại {refined_count} câu văn chuẩn xuất bản",
                    model=ed_model,
                )
                return parsed_edited
        except Exception as exc:
            logger.warning("Agent 4 (Editor) batch pass skipped (%s); keeping Agent 3 draft.", exc)

        return drafts

    async def polish_single_segment(
        self,
        src_text: str,
        draft_text: str,
        instruction: str = "",
        glossary: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        """
        Agent 4 on-demand single segment literary refinement.
        """
        if not src_text or not src_text.strip():
            return draft_text
        ed_override = self.config.agent_editor_model or self.config.model
        ed_provider = self._effective_provider(model_override=ed_override)
        matched_glossary = filter_glossary_for_text(src_text, glossary)
        if ed_provider == "google_free":
            return await self.translate_text(src_text, glossary=glossary)

        extra_prompt = resolve_genre_prompt(
            self.config.genre_preset, self.config.custom_prompt
        )
        if instruction and instruction.strip():
            extra_prompt = f"{extra_prompt}\nYêu cầu biên tập riêng cho đoạn này: {instruction.strip()}".strip()

        sys_prompt = build_editor_system_prompt(
            target_lang=self.config.target_lang,
            custom_prompt=extra_prompt,
            glossary=matched_glossary,
            is_batch=False,
        )
        user_prompt = (
            f"[SOURCE]: {src_text.strip()}\n"
            f"[DRAFT]: {(draft_text or '').strip()}\n\n"
            "Hãy biên tập và trả về duy nhất đoạn văn đã chuốt mượt chuẩn xuất bản:"
        )
        raw_out = await self._call_llm_with_rotation(
            sys_prompt, user_prompt, model_override=ed_override
        )
        cleaned = re.sub(r"^\[\[#\d+\]\]\s*", "", raw_out or "").strip()
        return cleaned or draft_text

    async def translate_batch(
        self,
        texts: List[str],
        context: str = "",
        glossary: Optional[List[Dict[str, str]]] = None,
    ) -> List[str]:
        """
        Multi-Agent Batch Pipeline:
        1. Checks SQLite cache for every item first.
        2. Agent 3 (Translator Swarm) translates uncached items with `[[#0]]`, `[[#1]]`... markers.
        3. Agent 4 (Chief Editor & Critic) reviews [SOURCE] + [DRAFT] and polishes prose (if enabled).
        4. Caches and returns 100%-aligned segments.
        """
        if not texts:
            return []

        tr_model_override = self.config.agent_translator_model or None
        provider = self._effective_provider(model_override=tr_model_override)
        model = self._effective_model(model_override=tr_model_override)
        ed_tag = f"+ed:{self.config.agent_editor_model or model}" if self.config.enable_editor_agent else ""
        cache_model_key = f"{model}{ed_tag}"
        target_lang = self.config.target_lang

        results: List[Optional[str]] = [None] * len(texts)
        uncached_indices: List[int] = []
        uncached_texts: List[str] = []

        for i, txt in enumerate(texts):
            if not txt or not txt.strip():
                results[i] = txt
                continue
            cached = self.cache.get(txt, target_lang, provider, cache_model_key)
            if cached is not None:
                results[i] = cached
            else:
                uncached_indices.append(i)
                uncached_texts.append(txt)

        if not uncached_texts:
            return [r if r is not None else "" for r in results]

        if len(uncached_texts) == 1:
            single_res = await self.translate_text(
                uncached_texts[0], context=context, glossary=glossary
            )
            results[uncached_indices[0]] = single_res
            return [r if r is not None else "" for r in results]

        combined_src = "\n\n".join(uncached_texts)
        matched_glossary = filter_glossary_for_text(combined_src, glossary)
        batch_user_prompt = build_batch_user_prompt(uncached_texts)

        parsed_segments: Optional[List[str]] = None

        async with self.semaphore:
            try:
                if provider == "google_free":
                    raw_translated = await self._translate_google_free_raw(
                        batch_user_prompt
                    )
                    parsed_segments = self._parse_batch_response(
                        raw_translated, len(uncached_texts)
                    )
                    if parsed_segments:
                        parsed_segments = [
                            self._apply_glossary_post_replacement(
                                seg, src, matched_glossary
                            )
                            for seg, src in zip(parsed_segments, uncached_texts)
                        ]
                else:
                    effective_prompt = resolve_genre_prompt(
                        self.config.genre_preset, self.config.custom_prompt
                    )
                    sys_prompt = build_system_prompt(
                        target_lang=target_lang,
                        custom_prompt=effective_prompt,
                        glossary=matched_glossary,
                        context=context,
                        is_batch=True,
                    )
                    raw_translated = await self._call_llm_with_rotation(
                        sys_prompt, batch_user_prompt, model_override=tr_model_override
                    )
                    parsed_segments = self._parse_batch_response(
                        raw_translated, len(uncached_texts)
                    )
                    if parsed_segments and len(parsed_segments) == len(uncached_texts):
                        await self._emit_agent_event(
                            agent_id=3,
                            agent_name="Agent 3 · Dịch Đa Luồng",
                            action="translated_batch",
                            detail=f"Hoàn thành bản dịch thô {len(uncached_texts)} đoạn văn (bàn giao cho Agent 4)",
                            model=model,
                        )
                        if self.config.enable_editor_agent:
                            parsed_segments = await self.refine_batch_with_editor(
                                uncached_texts, parsed_segments, matched_glossary
                            )
            except Exception as exc:
                logger.warning(
                    "Batch translation call failed (%s); falling back to parallel single segments.",
                    exc,
                )
                parsed_segments = None

        if parsed_segments and len(parsed_segments) == len(uncached_texts):
            cache_pairs: List[tuple[str, str]] = []
            for idx_in_batch, orig_idx in enumerate(uncached_indices):
                seg_dst = parsed_segments[idx_in_batch]
                results[orig_idx] = seg_dst
                cache_pairs.append((uncached_texts[idx_in_batch], seg_dst))
            self.cache.set_many(cache_pairs, target_lang, provider, cache_model_key)
        else:
            # Automatic 100% alignment fallback: translate each uncached item concurrently
            logger.info(
                "Batch marker count mismatch or error for %d segments; translating concurrently.",
                len(uncached_texts),
            )
            single_tasks = [
                self.translate_text(t, context=context, glossary=glossary)
                for t in uncached_texts
            ]
            single_outputs = await asyncio.gather(*single_tasks)
            for idx_in_batch, orig_idx in enumerate(uncached_indices):
                results[orig_idx] = single_outputs[idx_in_batch]

        return [r if r is not None else "" for r in results]

    async def extract_glossary(
        self, sample_text: str, max_terms: int = 35
    ) -> List[Dict[str, str]]:
        """
        Agent 2: Terminologist & Context Agent (Auto-Glossary Engine).
        Uses `agent_glossary_model` if configured.
        """
        if not sample_text or not sample_text.strip():
            return []

        gl_model_override = self.config.agent_glossary_model or None
        provider = self._effective_provider(model_override=gl_model_override)
        gl_model = self._effective_model(model_override=gl_model_override)

        if provider != "google_free":
            try:
                sys_prompt = (
                    "Bạn là Agent 2: Kỹ sư Thuật ngữ & Ngữ cảnh dịch thuật tự động. "
                    "Luôn trả về JSON Array hợp lệ."
                )
                user_prompt = build_glossary_extraction_prompt(
                    sample_text=sample_text,
                    target_lang=self.config.target_lang,
                    max_terms=max_terms,
                )
                async with self.semaphore:
                    raw_json = await self._call_llm_with_rotation(
                        sys_prompt, user_prompt, model_override=gl_model_override
                    )
                parsed = parse_llm_glossary_json(raw_json)
                if parsed:
                    await self._emit_agent_event(
                        agent_id=2,
                        agent_name="Agent 2 · Kỹ Sư Thuật Ngữ",
                        action="extracted_glossary",
                        detail=f"Trích xuất thành công {len(parsed[:max_terms])} thuật ngữ & quy tắc xưng hô cốt lõi",
                        model=gl_model,
                    )
                    return parsed[:max_terms]
            except Exception as exc:
                logger.warning(
                    "LLM glossary extraction failed (%s), falling back to heuristic extractor.",
                    exc,
                )

        # Heuristic extraction + Google Free translation of candidate terms
        candidates = extract_candidate_terms_heuristic(
            sample_text, max_terms=max_terms
        )
        if not candidates:
            return []

        src_terms = [c["src"] for c in candidates]
        try:
            joined = "\n".join(src_terms)
            translated_block = await self._translate_google_free_raw(joined)
            translated_lines = [
                line.strip() for line in translated_block.splitlines() if line.strip()
            ]
            if len(translated_lines) == len(candidates):
                for c, dst in zip(candidates, translated_lines):
                    c["dst"] = dst
        except Exception as exc:
            logger.debug("Heuristic glossary translation skipped: %s", exc)

        return normalize_glossary(candidates)[:max_terms]

    async def test_connection(self) -> Dict[str, Any]:
        """
        Tests the configured provider & API keys (for the UI "Test API" button).
        Returns latency, status, sample translation, and active key pool info.
        """
        import time

        start = time.perf_counter()
        provider = self._effective_provider()
        sample_src = "Knowledge is the compass that guides humanity through uncertainty."
        if provider == "gemini":
            keys_count = len(self.config.gemini_api_keys) or len(self.config.api_keys)
        elif provider == "hybrid":
            keys_count = len(self.config.api_keys) + len(self.config.gemini_api_keys)
        else:
            keys_count = len(self.config.api_keys)

        try:
            if provider == "google_free":
                translated = await self._translate_google_free_raw(sample_src)
            else:
                sys_prompt = build_system_prompt(
                    target_lang=self.config.target_lang, is_batch=False
                )
                translated = await self._call_llm_with_rotation(sys_prompt, sample_src)

            elapsed_ms = round((time.perf_counter() - start) * 1000)
            return {
                "ok": True,
                "provider": provider,
                "configured_provider": self.config.provider,
                "model": self._effective_model(),
                "keys_count": keys_count,
                "gateway_keys_count": len(self.config.api_keys),
                "gemini_keys_count": len(self.config.gemini_api_keys),
                "target_lang": self.config.target_lang,
                "latency_ms": elapsed_ms,
                "sample_source": sample_src,
                "sample_translation": translated,
                "message": f"Kết nối thành công tới {provider.upper()} ({self._effective_model()}) trong {elapsed_ms}ms!",
            }
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - start) * 1000)
            return {
                "ok": False,
                "provider": provider,
                "configured_provider": self.config.provider,
                "model": self._effective_model(),
                "keys_count": keys_count,
                "gateway_keys_count": len(self.config.api_keys),
                "gemini_keys_count": len(self.config.gemini_api_keys),
                "latency_ms": elapsed_ms,
                "error": str(exc),
                "message": f"Lỗi kết nối {provider.upper()}: {exc}",
            }

    async def discover_available_models(self) -> Dict[str, Any]:
        """
        Queries the configured Gateway /v1/models and Google Gemini /v1beta/models endpoints
        and merges them with our curated flagship model catalog so the user has a complete list.
        """
        gateway_models: List[str] = [
            "cc/claude-sonnet-5-5",
            "cc/claude-opus-5-5",
            "cc/claude-opus-5",
            "cc/claude-fable-5-1",
            "cc/claude-fable-5",
            "cc/claude-sonnet-5",
            "cc/claude-haiku-4-5-20251001",
        ]
        gemini_models: List[str] = [
            "gemini-2.5-pro",
            "gemini-ultra",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite-preview-06-17",
            "gemini-2.0-pro-exp-02-05",
            "gemini-2.0-flash",
            "gemini-2.0-flash-thinking-exp",
            "gemini-1.5-pro-latest",
            "gemini-1.5-flash-latest",
        ]
        other_models: List[str] = [
            "gpt-4o",
            "gpt-4o-mini",
            "o3-mini",
            "deepseek-chat",
            "deepseek-reasoner",
            "claude-3-7-sonnet-latest",
            "claude-3-5-sonnet-latest",
            "google/gemini-2.5-pro",
            "anthropic/claude-3.7-sonnet",
            "qwen2.5:14b",
            "gtx-free",
        ]

        # 1. Try live discovery from Gateway (/v1/models)
        base = (self.config.base_url or DEFAULT_BASE_URLS["openai"]).rstrip("/")
        gw_key = self.config.api_keys[0] if self.config.api_keys else ""
        if base:
            try:
                headers = {"Authorization": f"Bearer {gw_key}"} if gw_key else {}
                async with httpx.AsyncClient(timeout=8.0) as client:
                    r = await client.get(f"{base}/models", headers=headers)
                    if r.status_code == 200:
                        data = r.json().get("data") or []
                        for item in data:
                            mid = item.get("id") if isinstance(item, dict) else None
                            if mid and mid not in gateway_models:
                                gateway_models.append(mid)
            except Exception as exc:
                logger.debug("Gateway model discovery skipped: %s", exc)

        # 2. Try live discovery from Gemini API if gemini_api_keys provided
        gm_keys = self.config.gemini_api_keys or [
            k for k in self.config.api_keys if k.startswith("AIza")
        ]
        if gm_keys:
            gm_key = gm_keys[0]
            gm_base = (
                self.config.gemini_base_url or DEFAULT_BASE_URLS["gemini"]
            ).rstrip("/")
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    if gm_key.startswith("AIza") and "googleapis.com" in gm_base:
                        r = await client.get(f"{gm_base}/models?key={gm_key}")
                        if r.status_code == 200:
                            models_list = r.json().get("models") or []
                            for m in models_list:
                                name = str(m.get("name", "")).replace("models/", "")
                                methods = m.get("supportedGenerationMethods") or []
                                if (
                                    name.startswith("gemini")
                                    and "generateContent" in methods
                                    and name not in gemini_models
                                ):
                                    gemini_models.append(name)
                    else:
                        r = await client.get(
                            f"{gm_base}/models",
                            headers={"Authorization": f"Bearer {gm_key}"},
                        )
                        if r.status_code == 200:
                            data = r.json().get("data") or []
                            for item in data:
                                mid = item.get("id") if isinstance(item, dict) else None
                                if mid and mid not in gemini_models:
                                    gemini_models.append(mid)
            except Exception as exc:
                logger.debug("Gemini model discovery skipped: %s", exc)

        return {
            "ok": True,
            "gateway_models": gateway_models,
            "gemini_models": gemini_models,
            "other_models": other_models,
        }


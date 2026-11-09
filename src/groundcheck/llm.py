"""Real LLM adapters (stdlib only). Each is a callable: llm(prompt, system=None, max_tokens=800) -> str.
Keys come from the environment; nothing is logged. Retries 429/5xx with exponential backoff.
NOTE: written against the providers' documented REST shapes but not run live in the build sandbox."""
import json
import os
import time
import urllib.error
import urllib.request


class LLMError(RuntimeError):
    pass


def _post(url, headers, body):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


class _Base:
    name = "llm"

    def __init__(self, api_key, model, fetch=None, max_retries=4, sleep=time.sleep):
        if not api_key:
            raise LLMError(f"{self.name}: API key not set")
        self.api_key, self.model = api_key, model
        self.fetch, self.max_retries, self.sleep = fetch or _post, max_retries, sleep

    def _request(self, url, headers, payload):
        body = json.dumps(payload).encode()
        for attempt in range(self.max_retries + 1):
            status, text = self.fetch(url, headers, body)
            if status == 200:
                return json.loads(text)
            if status in (429, 500, 502, 503, 504, 529) and attempt < self.max_retries:
                self.sleep(2 ** attempt)
                continue
            raise LLMError(f"{self.name} error {status}: {text[:200]}")


class AnthropicLLM(_Base):
    name = "anthropic"
    URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, api_key=None, model=None, **kw):
        super().__init__(api_key or os.environ.get("ANTHROPIC_API_KEY"),
                         model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"), **kw)

    def __call__(self, prompt, system=None, max_tokens=800):
        payload = {"model": self.model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
        if system:
            payload["system"] = system
        data = self._request(self.URL, {"x-api-key": self.api_key, "anthropic-version": "2023-06-01",
                                        "content-type": "application/json"}, payload)
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


class GeminiLLM(_Base):
    name = "gemini"
    BASE = "https://generativelanguage.googleapis.com/v1beta/models/"

    def __init__(self, api_key=None, model=None, **kw):
        # Model ids change; set GEMINI_MODEL to a current one rather than trusting this default.
        super().__init__(api_key or os.environ.get("GEMINI_API_KEY"),
                         model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"), **kw)

    def __call__(self, prompt, system=None, max_tokens=800):
        url = f"{self.BASE}{self.model}:generateContent"
        payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": max_tokens}}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        data = self._request(url, {"x-goog-api-key": self.api_key, "content-type": "application/json"}, payload)
        cands = data.get("candidates") or []
        if not cands:
            raise LLMError(f"gemini returned no candidates: {str(data)[:200]}")
        return "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []))


def get_llm(provider=None, **kw):
    provider = (provider or os.environ.get("LLM_PROVIDER") or
                ("anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "gemini" if os.environ.get("GEMINI_API_KEY") else ""))
    if provider == "anthropic":
        return AnthropicLLM(**kw)
    if provider == "gemini":
        return GeminiLLM(**kw)
    raise LLMError("No LLM configured: set ANTHROPIC_API_KEY or GEMINI_API_KEY (or LLM_PROVIDER)")

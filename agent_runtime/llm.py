"""Model-agnostic LLM layer. Any OpenAI-compatible endpoint works (Ollama, llama.cpp, LM Studio, vLLM, hosted).

Small-model first: every prompt is fitted to a context budget (AGENT_CTX_TOKENS, default 8192) so a
3B model with a 16k window never overflows, and every call respects the mission's wall-clock budget.
"""
import json
import os
import re
import time
import urllib.request


class LLMError(Exception):
    pass


class BudgetExceeded(Exception):
    """Mission wall-clock budget is spent; the runtime turns this into an honest partial result."""


class LLM:
    def complete(self, system: str, prompt: str, max_tokens: int = 2000) -> str:
        raise NotImplementedError


def ctx_tokens():
    try:
        return max(2048, int(os.environ.get("AGENT_CTX_TOKENS", "8192")))
    except ValueError:
        return 8192


class _Fit:
    """fit(text) trims a prompt to the context budget, keeping head and tail (errors live at the end)."""
    CHARS_PER_TOKEN = 3.2

    def budget_chars(self, reserve_tokens=1200):
        return int((ctx_tokens() - reserve_tokens) * self.CHARS_PER_TOKEN)

    def __call__(self, text, max_chars=None):
        text = str(text)
        n = max_chars or self.budget_chars()
        if len(text) <= n:
            return text
        head = int(n * 0.6)
        return text[:head] + "\n…[trimmed to fit context]…\n" + text[-(n - head - 40):]


fit = _Fit()


class Budget:
    def __init__(self, seconds=None):
        self.deadline = time.time() + seconds if seconds else None

    def remaining(self):
        return float("inf") if self.deadline is None else self.deadline - time.time()

    def expired(self):
        return self.remaining() <= 0


class Metered(LLM):
    """Wraps any LLM: enforces the context budget and the mission deadline, and records prompt sizes."""

    def __init__(self, inner, budget=None):
        self.inner, self.budget = inner, budget or Budget()
        self.calls, self.max_prompt_tokens = 0, 0

    def complete(self, system, prompt, max_tokens=2000):
        if self.budget.expired():
            raise BudgetExceeded("mission time budget exhausted before LLM call")
        system = fit(system, fit.budget_chars() // 3)
        prompt = fit(prompt, fit.budget_chars() - len(system))
        self.calls += 1
        self.max_prompt_tokens = max(self.max_prompt_tokens, int((len(system) + len(prompt)) / fit.CHARS_PER_TOKEN))
        inner = self.inner
        if hasattr(inner, "timeout") and self.budget.deadline:
            inner.timeout = max(5, min(getattr(inner, "base_timeout", inner.timeout), int(self.budget.remaining())))
        return inner.complete(system, prompt, max_tokens)

    def __bool__(self):
        return True


class OpenAICompatLLM(LLM):
    def __init__(self, base_url, model, api_key="", timeout=None):
        # local models (Ollama/llama.cpp on modest hardware) can take minutes per call
        self.base, self.model, self.key = base_url.rstrip("/"), model, api_key
        self.timeout = timeout if timeout is not None else int(os.environ.get("AGENT_LLM_TIMEOUT", "600"))
        self.base_timeout = self.timeout

    def complete(self, system, prompt, max_tokens=2000):
        body = json.dumps({"model": self.model, "max_tokens": max_tokens, "temperature": 0,
                           "messages": [{"role": "system", "content": system},
                                        {"role": "user", "content": prompt}]}).encode()
        h = {"Content-Type": "application/json"}
        if self.key:
            h["Authorization"] = "Bearer " + self.key
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + "/chat/completions", body, h),
                                        timeout=self.timeout) as r:
                return json.load(r)["choices"][0]["message"]["content"]
        except Exception as e:
            raise LLMError(str(e)) from e


def from_env(prefix="AGENT_LLM"):
    """AGENT_LLM_BASE_URL (e.g. http://localhost:11434/v1), AGENT_LLM_MODEL, AGENT_LLM_API_KEY.
    prefix='AGENT_JUDGE' configures an optional separate verifier model."""
    base = os.environ.get(f"{prefix}_BASE_URL")
    if not base:
        return None
    return OpenAICompatLLM(base, os.environ.get(f"{prefix}_MODEL", "llama3.1"), os.environ.get(f"{prefix}_API_KEY", ""))


def extract_json(text: str):
    text = re.sub(r"```(?:json)?", "", text or "")
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                return dec.raw_decode(text[i:])[0]
            except ValueError:
                continue
    raise LLMError("no JSON found in model output")

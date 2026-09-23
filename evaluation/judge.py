"""An LLM judge that scores one claim at a time, from outside the system under test.

Three rules make the score worth reading:

* **The judge never runs through the pipeline it judges.** It calls the provider directly,
  not `/api/chat`, so a retrieval bug cannot quietly help the grader agree with the answer.
* **A different model family than the answer.** Asking a model to mark its own homework
  measures self-consistency, not correctness; the default judge is Qwen while answers come
  from Llama, and the runner refuses a same-family pair unless `--allow-same-family`.
* **One verdict per claim, with the evidence attached.** The judge sees the reference fact,
  the answer and the passages the pipeline retrieved, and returns `supported`,
  `contradicted` or `missing` for that single fact - plus whether the answer asserts
  anything the passages do not support, which is the hallucination signal.

The judge is a local model by default, so judging a confidential answer never sends it off
the machine - the same rule the assistant itself follows (D-15).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Literal

import httpx

Verdict = Literal["supported", "contradicted", "missing"]
FAMILY = {
    "llama": "meta",
    "qwen": "alibaba",
    "gpt-oss": "openai",
    "nemotron": "nvidia",
    "mistral": "mistral",
    "phi": "microsoft",
    "gemma": "google",
    "claude": "anthropic",
}

JUDGE_PROMPT = """You grade one claim about an internal assistant's answer.

You are given: the question, ONE reference fact that a correct answer must convey, the
assistant's answer, and the passages the assistant retrieved.

Reply with ONLY this JSON object, no prose:
{"verdict": "supported" | "contradicted" | "missing",
 "quote": "the exact words from the ANSWER that justify the verdict, copied exactly",
 "unsupported_claims": ["..."],
 "note": "one short sentence"}

- "supported": the answer conveys the reference fact. Wording may differ and the answer may
  say more; what matters is that the fact is there and the numbers match.
- "contradicted": the answer asserts something incompatible with the reference fact.
- "missing": the answer neither conveys nor contradicts it. An answer that declines, says it
  could not find the information, or answers a different question is ALWAYS "missing" - never
  "contradicted", because it asserts nothing.

Examples:
- fact "Employees receive 14 days", answer "Employees receive 14 days after confirmation."
  -> supported
- fact "Employees receive 14 days", answer "Employees receive 21 days." -> contradicted
- fact "Employees receive 14 days", answer "I could not find this in the approved knowledge."
  -> missing

For "contradicted" the quote must be copied from the ANSWER, character for character - not
from the reference fact, and not paraphrased. A contradiction that cannot be quoted from the
answer is discarded, so if you cannot quote it, the verdict is "missing".
- "unsupported_claims": specific statements in the answer that the passages do not support.
  An empty list means everything the answer asserts is traceable to a passage. Ignore
  hedging, greetings and offers to help; list only factual assertions.
"""

GROUNDING_PROMPT = """You check whether an assistant's answer stays inside its sources.

You are given the question, the answer, and the passages the assistant retrieved.

Reply with ONLY this JSON object, no prose:
{"unsupported_claims": ["..."], "note": "one short sentence"}

List every factual statement in the answer that the passages do not support. An answer that
says the information could not be found, and asserts nothing else, has no unsupported claims.
"""


@dataclass(frozen=True)
class Judgement:
    verdict: Verdict | None
    unsupported: list[str]
    note: str
    error: str | None = None
    quote: str = ""
    unverified: bool = False
    """True when the judge claimed a verdict it could not quote from the answer."""


def _quotes(quote: str, answer: str) -> bool:
    """Is the judge's quote really in the answer? Whitespace and case are forgiven, nothing
    else is - and a quote shorter than a few characters proves nothing."""
    if len(quote) < 8:
        return False

    def normalize(text: str) -> str:
        return " ".join(text.lower().split())

    return normalize(quote) in normalize(answer)


def family_of(model: str) -> str:
    name = model.split("/", 1)[-1].lower()
    for key, fam in FAMILY.items():
        if key in name:
            return fam
    return name


class Judge:
    """Talks to Ollama or an OpenAI-compatible endpoint (NIM) directly."""

    def __init__(self, model: str, timeout: float = 120.0) -> None:
        self.model = model
        provider, _, name = model.partition("/")
        self.provider, self.name = provider, name or provider
        self.client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self.client.close()

    def _complete(self, system: str, user: str, json_mode: bool = True) -> str:
        if self.provider == "ollama":
            base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
            resp = self.client.post(
                f"{base}/api/chat",
                json={
                    "model": self.name,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "stream": False,
                    "options": {"temperature": 0.0},
                    **({"format": "json"} if json_mode else {}),
                },
            )
            resp.raise_for_status()
            return str(resp.json()["message"]["content"])
        if self.provider == "nim":
            key = os.environ.get("NVIDIA_API_KEY")
            if not key:
                raise RuntimeError("NVIDIA_API_KEY is not set; the NIM judge cannot run")
            resp = self.client.post(
                "https://integrate.api.nvidia.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": self.name,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 400,
                },
            )
            resp.raise_for_status()
            return str(resp.json()["choices"][0]["message"]["content"])
        raise RuntimeError(f"unsupported judge provider: {self.provider!r}")

    @staticmethod
    def _parse(text: str) -> dict[str, Any]:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise ValueError(f"no JSON in judge reply: {text[:120]!r}")
        data = json.loads(match.group(0))
        if not isinstance(data, dict):
            raise ValueError("judge reply is not an object")
        return data

    def judge_fact(self, question: str, fact: str, answer: str, sources: list[str]) -> Judgement:
        user = (
            f"QUESTION:\n{question}\n\nREFERENCE FACT:\n{fact}\n\n"
            f"ANSWER:\n{answer or '(empty)'}\n\nRETRIEVED PASSAGES:\n"
            + ("\n---\n".join(sources) if sources else "(none)")
        )
        try:
            data = self._parse(self._complete(JUDGE_PROMPT, user))
        except Exception as err:  # a judge failure is data, not a crash
            return Judgement(None, [], "", error=f"{type(err).__name__}: {err}")
        verdict = data.get("verdict")
        if verdict not in ("supported", "contradicted", "missing"):
            return Judgement(None, [], "", error=f"unusable verdict: {verdict!r}")
        claims = [str(c) for c in data.get("unsupported_claims", []) if str(c).strip()]
        quote = str(data.get("quote", "")).strip()
        # `contradicted` is the destructive verdict - it says the assistant asserted
        # something false - so it must be evidenced: the contradicting words have to be in
        # the answer. This is what caught the judge marking *correct* answers as
        # contradicted, having read the reference fact's own "not 21" as if the assistant
        # had written it. `supported` is not held to the same rule, because conveying a fact
        # in different words is exactly what a judge exists to recognise.
        if verdict == "contradicted" and not _quotes(quote, answer):
            return Judgement(
                None,
                claims,
                str(data.get("note", ""))[:200],
                error=f"unquotable {verdict}: {quote[:80]!r}",
                quote=quote,
                unverified=True,
            )
        return Judgement(verdict, claims, str(data.get("note", ""))[:200], quote=quote)

    def judge_grounding(self, question: str, answer: str, sources: list[str]) -> Judgement:
        """For cases with no reference fact: does the answer stay inside its sources?"""
        user = (
            f"QUESTION:\n{question}\n\nANSWER:\n{answer or '(empty)'}\n\nRETRIEVED PASSAGES:\n"
            + ("\n---\n".join(sources) if sources else "(none)")
        )
        try:
            data = self._parse(self._complete(GROUNDING_PROMPT, user))
        except Exception as err:
            return Judgement(None, [], "", error=f"{type(err).__name__}: {err}")
        claims = [str(c) for c in data.get("unsupported_claims", []) if str(c).strip()]
        return Judgement(None, claims, str(data.get("note", ""))[:200])

    def answer_without_retrieval(self, question: str) -> str:
        """Control baseline: the same class of model, asked the same question with no
        documents and no policy. It is what the assistant would be without any of this."""
        try:
            return self._complete(
                "You are an internal company assistant. Answer the employee's question.",
                question,
                json_mode=False,
            )
        except Exception as err:
            return f"(control model failed: {type(err).__name__})"

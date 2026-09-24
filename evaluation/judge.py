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

# Three graders, three separate questions, so a failure points at one cause:
#   JUDGE      - does the answer convey this one reference fact?
#   CITATION   - does this one passage support this one sentence?
#   GROUNDING  - did the answer assert anything the retrieved passages do not support?
# A fact judgement never lists unsupported claims (that is grounding's job), and grounding
# never looks at the reference fact. Worked examples use a made-up domain on purpose:
# nothing here may resemble a graded case (tests/unit/test_prompt_hygiene.py, D-34).

JUDGE_PROMPT = """You grade ONE reference fact against an internal assistant's answer.

You are given the QUESTION, ONE REFERENCE FACT that a correct answer must convey, the
ANSWER, and the PASSAGES the assistant retrieved.

Reply with ONLY this JSON object, no prose:
{"verdict": "supported" | "contradicted" | "missing",
 "quote": "exact words copied from the ANSWER, or an empty string",
 "note": "one short sentence"}

Verdicts:
- "supported": the answer conveys the reference fact, directly or in equivalent words.
  Equivalent forms of the same value count: 08:00 and 8 AM, 1,000 and 1000, 10% and one in
  ten. Anything else the answer says does not change this verdict unless it directly
  contradicts the reference fact.
- "contradicted": the answer explicitly asserts something incompatible with the reference
  fact, about the same entity, time, place, scope and condition.
- "missing": the answer neither conveys nor contradicts the reference fact.

Rules:
- An answer that declines, says it could not find the information, or answers a different
  question is "missing" - unless it also explicitly asserts a contradicting fact.
- A different value under a different entity, time period, place, scope or condition is not
  a contradiction. It is "missing" if the fact itself is not conveyed.
- For "supported" and "contradicted", "quote" is copied character for character from the
  ANSWER - never from the reference fact, never paraphrased. If no text in the answer
  establishes a contradiction, the verdict is "missing". For "missing", "quote" is "".
- Judge only the reference fact. Whether other statements are grounded is checked
  separately.

Examples (made-up domain):
- fact "The reading room opens at 08:00"; answer "It opens at 8 AM on weekdays." -> supported
- fact "The reading room opens at 08:00"; answer "It opens at 10:00." -> contradicted
- fact "The reading room opens at 08:00"; answer "On public holidays it opens at 10:00."
  -> missing (a different condition, and the fact itself is not conveyed)
- fact "The reading room opens at 08:00"; answer "I could not find this." -> missing
"""

CITATION_PROMPT = """You check ONE citation.

You are given a PASSAGE that an assistant cited and the STATEMENT the citation was attached
to. Decide whether a reasonable reader who opened that passage would find the statement
supported by it.

Reply with ONLY this JSON object, no prose:
{"supports": true | false, "note": "one short sentence"}

The passage must support the specific claim, not just the topic. Be strict about:
entities, relationships, numbers and quantities, dates and time periods, places, scope, and
cause and effect. A passage that says B happened after A does not support "A caused B".
Paraphrase and equivalent wording are fine; added specifics, implications or conclusions the
passage does not state are not.
"""

GROUNDING_PROMPT = """You check whether an assistant's answer stays within its retrieved
passages.

You are given the QUESTION, the ANSWER and the RETRIEVED PASSAGES.

Reply with ONLY this JSON object, no prose:
{"unsupported_claims": ["..."], "note": "one short sentence"}

List every factual statement in the answer that the passages neither state nor reasonably
entail. Paraphrases, summaries and less specific restatements of supported facts are
grounded; do not require the same wording. Look especially for entities, relationships,
numbers, dates, places, scope, causes, and comparisons or conclusions presented as fact
that the passages do not support.

Before you list a statement, look for it in the passages. If the passages state it, even in
other words, it is grounded and must not be listed. Most answers that stay close to their
sources have no unsupported claims at all: an empty list is the expected result for them.

Do not list greetings, hedging, opinions, recommendations, offers to help, or statements
that the information could not be found.
"""


_STOP = frozenset(
    re.findall(
        r"\S+",
        "a an the of to in on for at by with and or is are was were be been it its this that "
        "these those as from according source sources may must can",
    )
)


def _stem(word: str) -> str:
    """Crude on purpose: "updated", "updates" and "update" should count as the same word."""
    for suffix in ("ing", "ed", "es", "s"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            word = word[: -len(suffix)]
            break
    return word[:-1] if len(word) > 4 and word.endswith("e") else word


def _content_words(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9][a-z0-9.,:%-]*[a-z0-9%]|[a-z0-9]", text.lower())
    return [_stem(w.strip(".,:")) for w in words if w not in _STOP]


def _found_in(claim: str, sources: list[str]) -> bool:
    """True when at least 80% of the claim's content words appear in one passage: the claim
    is then visibly in the retrieved text, and calling it unsupported is a grader error. Every
    number in the claim must also be in that passage, so a changed figure is always kept."""
    words = _content_words(claim)
    if not words:
        return True
    figures = set(re.findall(r"\d+(?:[.,:]\d+)*%?", claim))
    for passage in sources:
        present = set(_content_words(passage))
        # Every figure must be in the passage: "30 minutes" against "60 minutes" is exactly
        # the kind of claim the grounding check exists to catch.
        if figures - set(re.findall(r"\d+(?:[.,:]\d+)*%?", passage)):
            continue
        if sum(w in present for w in words) / len(words) >= 0.8:
            return True
    return False


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
        claims: list[str] = []  # grounding is judged separately (judge_grounding)
        quote = str(data.get("quote", "")).strip()
        # `contradicted` is the destructive verdict - it says the assistant asserted
        # something false - so it must be evidenced: the contradicting words have to be in
        # the answer. This is what caught the judge marking *correct* answers as
        # contradicted, having read the reference fact's own "not 21" as if the assistant
        # had written it. `supported` is not held to the same rule, because conveying a fact
        # in different words is exactly what a judge exists to recognise.
        # Measured on the D5 answers: the 7B judge's remaining errors were "contradicted"
        # verdicts whose own quote states the reference fact (K08, M03, M05). Evidence that
        # conveys the fact cannot show a contradiction of it, so such a verdict is discarded
        # like an unquotable one. A targeted worked example was tried first and made things
        # worse (35/39 vs 36/39), so the check is in code, not in the prompt.
        if verdict == "contradicted" and _found_in(fact, [quote]):
            return Judgement(
                None,
                claims,
                str(data.get("note", ""))[:200],
                error=f"contradiction quote conveys the fact: {quote[:80]!r}",
                quote=quote,
                unverified=True,
            )
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
        """Does the answer stay inside its sources? Asked of every answer, with or without a
        reference fact, and independently of whether that fact is conveyed."""
        user = (
            f"QUESTION:\n{question}\n\nANSWER:\n{answer or '(empty)'}\n\nRETRIEVED PASSAGES:\n"
            + ("\n---\n".join(sources) if sources else "(none)")
        )
        try:
            data = self._parse(self._complete(GROUNDING_PROMPT, user))
        except Exception as err:
            return Judgement(None, [], "", error=f"{type(err).__name__}: {err}")
        claims = [str(c) for c in data.get("unsupported_claims", []) if str(c).strip()]
        # Like the quote rule for "contradicted": a claim the passages visibly contain is not
        # unsupported, whatever the judge says. Measured: before this check the grounding
        # judge flagged one "unsupported" claim in 31 of 31 answers, most of them verbatim
        # in the retrieved text (e.g. "Tuesday or Thursday").
        kept = [c for c in claims if not _found_in(c, sources)]
        return Judgement(None, kept, str(data.get("note", ""))[:200])

    def judge_citation(self, claim: str, snippet: str) -> bool | None:
        """Does *this* passage support *this* sentence?

        The brief asks that a citation support the exact claim, not merely that the document
        be relevant. A reader clicks `[1]`, lands on the quoted passage and must find the
        statement there; anything else is a citation that looks right and is not.
        Returns True / False, or None when the judge gave no usable answer.
        """
        user = f"PASSAGE:\n{snippet}\n\nSTATEMENT:\n{claim}"
        try:
            data = self._parse(self._complete(CITATION_PROMPT, user))
        except Exception:
            return None
        verdict = data.get("supports")
        return bool(verdict) if isinstance(verdict, bool) else None

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

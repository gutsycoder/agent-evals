"""Solver implementations: stub, oracle, and a provider-pluggable LLM solver.

The LLM solver is split in two layers so that swapping model vendors is trivial:

  - ``LLMSolver`` owns everything provider-agnostic: building the prompt from the
    bundle's description + repo files, extracting a clean unified diff from the
    model's reply, and recording run metadata.
  - ``LLMProvider`` implementations own exactly one thing: text in -> text out.
    Adding a new vendor means writing one small class with a ``complete`` method
    and registering it in ``PROVIDERS``. Nothing else in the harness changes.

Provider/model selection precedence: CLI flags > env vars (TASKCLI_PROVIDER,
TASKCLI_MODEL) > defaults (gemini, per-provider default model below).

Credentials use each ecosystem's standard mechanism: boto3's default chain for
bedrock (AWS_* env vars / ~/.aws), ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY.
SDK imports are lazy so only the provider actually used needs its package installed.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Optional, Protocol

# `@@ -old[,count] +new[,count] @@[ trailing context]` - counts are optional in the format
# (absent means 1), and the trailing text after the second @@ is preserved verbatim.
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
# A bare XML-ish wrapper tag at column zero, e.g. `<diff>` / `</diff>` / `<patch>`. Anchored
# with no leading whitespace on purpose: every real line inside a hunk carries a ' ', '+' or
# '-' prefix, so this cannot match a genuine `</div>` in an HTML diff.
# Files that are prose or legal text, never the subject of a code fix. Ranked below even
# "nothing matches" so they lose the last of the budget rather than winning it: ties break
# alphabetically, and at a repo root CHANGELOG.md and LICENSE sort ahead of every source
# directory. On one real run they took 87KB of a 347KB prompt between them.
_NON_CODE_RE = re.compile(
    r"^(CHANGELOG|CHANGES|HISTORY|LICEN[SC]E|COPYING|NOTICE|AUTHORS|CONTRIBUTORS|"
    r"CONTRIBUTING|CODE_OF_CONDUCT|SECURITY|PATENTS)(\.[A-Za-z]+)?$",
    re.IGNORECASE,
)

_WRAPPER_TAG_RE = re.compile(r"^</?[A-Za-z][A-Za-z0-9_-]*>$")


class FileReader(Protocol):
    """Read-only view of the repo *inside the solve container*.

    Solvers never receive a host path: repo bytes are fetched on demand via `docker exec`
    and only the files a solver actually inlines ever cross to the host.
    """

    def list_files(self) -> list[str]: ...

    # max_bytes is explicit because the reader must not decide truncation on the caller's
    # behalf - a silently truncated file is indistinguishable from a complete one, which is
    # exactly how a model ended up inventing code it could not see.
    def read_many(self, rel_paths: list[str], max_bytes: Optional[int] = None) -> dict[str, str]: ...


class Solver(Protocol):
    def solve(self, files: FileReader, description: str) -> str: ...  # returns unified diff


class StubSolver:
    """Produces no changes. Useful for exercising the harness end-to-end."""

    def solve(self, files: FileReader, description: str) -> str:
        return ""


class OracleSolver:
    """"Cheats" by returning the bundle's own golden patch, for validating the grading pipeline itself."""

    def __init__(self, patch_text: str) -> None:
        self.patch_text = patch_text

    def solve(self, files: FileReader, description: str) -> str:
        return self.patch_text


# ---------------------------------------------------------------------------
# LLM providers: one class per vendor, each exposing complete(prompt) -> str
# ---------------------------------------------------------------------------

class LLMProvider(Protocol):
    model: str

    def complete(self, prompt: str) -> str: ...


# Shared fallback output cap. Each provider sets its own MAX_OUTPUT_TOKENS, because the real
# ceiling is model-specific - gpt-4o hard-caps at 16384 while Gemini 2.5 Flash allows far more -
# so this must not be raised globally as a shortcut.
DEFAULT_MAX_OUTPUT_TOKENS = 8192


class BedrockProvider:
    """AWS Bedrock via boto3's provider-agnostic `converse` API.

    Model IDs vary by region and by which models the account has enabled -
    run `aws bedrock list-foundation-models` (or check the Bedrock console's
    Model access page) and override with --model / TASKCLI_MODEL if the
    default isn't available on your account.
    """

    DEFAULT_MODEL = "anthropic.claude-sonnet-5"
    # Conservative: the real ceiling depends on which model the account has enabled.
    MAX_OUTPUT_TOKENS = DEFAULT_MAX_OUTPUT_TOKENS

    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model or self.DEFAULT_MODEL

    def complete(self, prompt: str) -> str:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

        try:
            client = boto3.client("bedrock-runtime")
            response = client.converse(
                modelId=self.model,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": self.MAX_OUTPUT_TOKENS},
            )
        except NoCredentialsError as exc:
            raise RuntimeError(
                "No AWS credentials found. Set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY "
                "(or configure a profile via `aws configure`)."
            ) from exc
        except (ClientError, BotoCoreError) as exc:
            raise RuntimeError(
                f"Bedrock call failed (model={self.model}): {exc}\n"
                "Check that your IAM user has bedrock:InvokeModel permission and that this "
                "model is enabled in the Bedrock console (Model access) for your region. "
                "List usable ids with: aws bedrock list-foundation-models"
            ) from exc

        parts = response.get("output", {}).get("message", {}).get("content", [])
        return "".join(p.get("text", "") for p in parts)


class AnthropicProvider:
    """Anthropic first-party API via the official `anthropic` SDK."""

    DEFAULT_MODEL = "claude-opus-5"
    # Claude models comfortably exceed the shared default; a multi-file diff needs the room.
    MAX_OUTPUT_TOKENS = 32_768

    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model or self.DEFAULT_MODEL

    def complete(self, prompt: str) -> str:
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError(
                "The 'anthropic' package is not installed. Run: pip install anthropic"
            ) from exc

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Export it to use --provider anthropic."
            )

        client = anthropic.Anthropic()
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=self.MAX_OUTPUT_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError as exc:
            raise RuntimeError(f"Anthropic API call failed (model={self.model}): {exc}") from exc

        if response.stop_reason == "refusal":
            raise RuntimeError(
                f"Anthropic model {self.model} declined the request (stop_reason=refusal)."
            )
        return "".join(b.text for b in response.content if b.type == "text")


class OpenAIProvider:
    """OpenAI API via the official `openai` SDK."""

    DEFAULT_MODEL = "gpt-4o"
    # gpt-4o hard-caps completions at 16384; requesting more is rejected outright.
    MAX_OUTPUT_TOKENS = 16_384

    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model or self.DEFAULT_MODEL

    def complete(self, prompt: str) -> str:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "The 'openai' package is not installed. Run: pip install openai"
            ) from exc

        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Export it to use --provider openai."
            )

        client = OpenAI()
        try:
            response = client.chat.completions.create(
                model=self.model,
                max_completion_tokens=self.MAX_OUTPUT_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            raise RuntimeError(f"OpenAI API call failed (model={self.model}): {exc}") from exc

        return response.choices[0].message.content or ""


# Finish reasons that mean "the response is complete and usable". STOP is normal completion;
# FINISH_REASON_UNSPECIFIED is the proto default and carries no evidence of truncation, so
# treating it as a failure would reject perfectly good responses.
_GEMINI_OK_FINISH_REASONS = frozenset({"STOP", "FINISH_REASON_UNSPECIFIED"})

# Per-reason guidance, so the error names the cause that actually occurred instead of listing
# every possibility. Deliberately not exhaustive: the enum gains values over time (there are
# already image-specific ones), and an unknown reason still needs to fail loudly rather than
# be mistaken for a recitation block - see the fallback in check_gemini_finish_reason.
_GEMINI_FINISH_REASON_HELP: dict[str, str] = {
    "RECITATION": (
        "The output too closely matched known training content, so generation was cut short. "
        "Plausible for this task, which asks the model to reproduce real open-source code "
        "near-verbatim as a diff. A retry at higher temperature is attempted automatically; "
        "if it still fails, try a different model or provider."
    ),
    "MAX_TOKENS": (
        "The response hit the output-token cap before finishing. Either the fix is genuinely "
        "too large to emit as one diff, or thinking tokens consumed the budget - Gemini 2.5 "
        "draws reasoning and visible output from the same allowance. Raise "
        "GeminiProvider.MAX_OUTPUT_TOKENS, or set THINKING_BUDGET=0 to disable thinking."
    ),
    "SAFETY": "The provider's safety filters withheld the response.",
    "BLOCKLIST": "The output matched a provider blocklist and was withheld.",
    "PROHIBITED_CONTENT": "The provider classified the output as prohibited content.",
    "SPII": "The output was withheld for possibly containing sensitive personal information.",
    "LANGUAGE": "The response language is unsupported.",
    "MALFORMED_FUNCTION_CALL": "The model emitted an invalid function call.",
    "OTHER": "The provider stopped generation without giving a specific reason.",
}


def _is_transient(exc: Any) -> bool:
    """Is this a provider-side capacity blip worth waiting out, rather than a bad request?

    Only server-overload conditions qualify. 429 (quota) is excluded on purpose: it clears on
    a daily boundary, not in seconds, so retrying only burns whatever allowance is left.
    """
    text = str(exc)
    # 502 is here because a real run died on one: Gemini returned a Bad Gateway HTML error
    # page, which is as transient as the 503 beside it, but the run failed outright instead of
    # waiting four seconds and succeeding.
    return any(m in text for m in (
        "503", "UNAVAILABLE", "500", "INTERNAL", "502", "Bad Gateway", "overloaded"))


def _usage_summary(response: Any) -> dict[str, Any]:
    """Token accounting from a Gemini response, for the run report.

    Reasoning tokens are billed from the SAME allowance as the visible answer, so without this
    a MAX_TOKENS failure is indistinguishable between "the fix was enormous" and "the model
    looped while thinking and wrote nothing". Those need opposite responses, and guessing
    between them cost real debugging time.
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return {}
    return {
        "prompt_tokens": getattr(usage, "prompt_token_count", None),
        "thinking_tokens": getattr(usage, "thoughts_token_count", None),
        "output_tokens": getattr(usage, "candidates_token_count", None),
    }


def _gemini_api_error(exc: Any, model: str) -> str:
    """Message for a failed Gemini call, naming the fix when the cause is recognisable.

    Free-tier quota is the one a first-time user is most likely to meet, and a bare 429 does not say
    that quotas are PER MODEL - the actionable part, since switching model beats waiting a day.
    """
    text = str(exc)
    if "429" in text or "RESOURCE_EXHAUSTED" in text or "quota" in text.lower():
        return (
            f"Gemini rate limit / quota exhausted (model={model}).\n"
            f"  Free-tier quotas are per model per day, so switching is usually faster than "
            f"waiting: --model gemini-3.1-flash-lite has a much higher daily allowance than "
            f"gemini-2.5-flash.\n"
            f"  A completed run's diff is stored, so `evals replay <run_id>` re-grades it "
            f"without spending another request.\n  Original: {text}"
        )
    if _is_transient(exc):
        return (
            f"Gemini is temporarily unavailable (model={model}) and did not recover after "
            f"retrying. This is provider-side load, not a problem with the request. Try again "
            f"shortly, or --model a less busy one.\n  Original: {text}"
        )
    if "API key" in text or "API_KEY_INVALID" in text or "401" in text or "403" in text:
        return (
            f"Gemini rejected the credentials (model={model}). Check GEMINI_API_KEY is set and "
            f"valid - get one free at https://aistudio.google.com/apikey.\n  Original: {text}"
        )
    if "NOT_FOUND" in text or "404" in text:
        return (
            f"Gemini does not recognise model {model!r}. Check the id against "
            f"https://ai.google.dev/gemini-api/docs/models.\n  Original: {text}"
        )
    return f"Gemini API call failed (model={model}): {text}"


def check_gemini_finish_reason(finish_reason: Any, model: str) -> None:
    """Raise a clear, cause-specific error if a Gemini response didn't finish normally.

    Gemini signals truncated or withheld output via `finish_reason` rather than an exception,
    so ignoring it means treating a partial response as a complete answer. That was a real
    failure: a diff cut off mid-line surfaced downstream as a generic "corrupt patch" from
    `git apply`, with nothing pointing at the actual cause.
    """
    if finish_reason is None:
        return
    # The SDK enum is a str subclass whose str() is "FinishReason.RECITATION", so .name is
    # the reliable accessor; the rsplit fallback covers a plain string or an SDK change.
    name = (getattr(finish_reason, "name", None) or str(finish_reason).rsplit(".", 1)[-1]).upper()
    if name in _GEMINI_OK_FINISH_REASONS:
        return

    explanation = _GEMINI_FINISH_REASON_HELP.get(
        name,
        "This reason is not one the harness has specific guidance for - check Gemini's "
        "FinishReason documentation for what it means.",
    )
    raise RuntimeError(
        f"Gemini response did not finish normally (finish_reason={name}, model={model}): "
        f"the text returned is truncated or withheld, not a complete answer.\n  {explanation}"
    )


class GeminiProvider:
    """Google Gemini via the official `google-genai` SDK.

    Temperature deserves a note, because it is a genuine tradeoff rather than a free win.
    A code fix is not creative writing: temperature 0 makes a run reproducible, and
    reproducibility is a property this harness is explicitly built around (pinned commits,
    pinned images, `replay` for re-grading stored diffs). So 0.0 is the default.

    But temperature 0 also maximizes the chance of emitting *verbatim* memorized text, which
    is exactly what trips Gemini's RECITATION filter when the task is "reproduce real
    open-source code as a diff" - observed on ansible-vars-001. Raising the temperature adds
    enough entropy to make an exact-match block less likely.

    Rather than trade determinism away on every call for a benefit that only matters on the
    rare recitation failure, the first attempt is deterministic and a *retry* uses the higher
    temperature - and only when RECITATION actually occurred. Normal runs stay reproducible;
    the pathological case gets the entropy it needs. Worth being honest that the retry's
    effectiveness is inferred from how the filter works, not something documented and
    guaranteed by Google; if it fails the error is still surfaced clearly rather than hidden.
    """

    DEFAULT_MODEL = "gemini-3.1-flash-lite"
    # Deterministic first attempt; retry temperature used only after a RECITATION block.
    TEMPERATURE = 0.0
    RECITATION_RETRY_TEMPERATURE = 0.2

    # Raised well above the shared 8192 default after a real MAX_TOKENS failure on
    # ansible-vars-001. Two things were wrong at once:
    #
    #  1. Gemini 2.5 models think by default, and thinking tokens are drawn from the SAME
    #     output budget as the visible answer. The model spent the entire 8192 on reasoning
    #     and got cut off before finishing the diff - the returned text was a truncated hunk,
    #     which is why this first looked like a malformed-diff problem rather than a budget one.
    #  2. 8192 is genuinely too small for this task regardless. A real multi-file fix, emitted
    #     as a unified diff with full context lines, is easily thousands of tokens.
    #
    # Thinking is ENABLED but explicitly bounded. Disabling it entirely was a mistake: the
    # observed errors - renaming a variable then still referencing the old name in the same
    # hunk, importing a class from the wrong module - are exactly the self-inconsistencies a
    # reasoning pass catches. What actually caused the MAX_TOKENS failure was the 8192 cap,
    # not thinking itself; with 32768 there is room for reasoning AND a multi-file diff.
    # The budget is bounded rather than AUTOMATIC (-1) so reasoning can never consume the
    # whole allowance and silently starve the answer, which is the failure mode that is
    # indistinguishable from a formatting bug.
    MAX_OUTPUT_TOKENS = 65_536          # the model's hard ceiling, confirmed via the API
    THINKING_BUDGET = 8_192
    # Set by --thinking; None means the per-family default (medium).
    THINKING_OVERRIDE: Optional[str] = None
    # Provider-capacity blips (503/500). Popular models return these under load, and a run
    # that dies on one throws away the container work already done for it.
    MAX_TRANSIENT_RETRIES = 3
    TRANSIENT_BACKOFF_SECONDS = 4

    def __init__(self, model: Optional[str] = None) -> None:
        self.model = model or self.DEFAULT_MODEL
        # Surfaced in run metadata so a report answers "what varied between these two runs?"
        self.last_temperature: Optional[float] = None
        self.last_retried = False
        self.last_thinking: Optional[str] = None
        self.last_usage: dict[str, Any] = {}

    def _effective_thinking(self, retry_state: str) -> str:
        """The reasoning level actually sent, resolving --thinking against the retry state.

        `retry_state` is the retry loop's own signal ('normal' | 'minimal' | 'off'), which is
        not a level: 'normal' only means "nothing has gone wrong yet", and the level in that
        case comes from --thinking. Recording the retry state as though it were the level made
        every report read thinking="normal" no matter what was requested, so `--thinking high`
        and the default produced artifacts that were indistinguishable - and the artifact
        exists to answer exactly that question.
        """
        if retry_state in ("off", "minimal"):
            return retry_state
        return self.THINKING_OVERRIDE or "medium"

    def complete(self, prompt: str) -> str:
        try:
            from google import genai
            from google.genai import errors, types
        except ImportError as exc:
            raise RuntimeError(
                "The 'google-genai' package is not installed. Run: pip install google-genai"
            ) from exc

        if not os.environ.get("GEMINI_API_KEY"):
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Export it to use --provider gemini. "
                "Free keys: https://aistudio.google.com/apikey"
            )

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

        def _config(temperature: float, thinking: str) -> Any:
            """`thinking`: 'normal' | 'minimal' | 'off'."""
            kwargs: dict[str, Any] = {
                "max_output_tokens": self.MAX_OUTPUT_TOKENS, "temperature": temperature,
            }
            if thinking == "off":
                return types.GenerateContentConfig(**kwargs)
            # The two families take DIFFERENT knobs and silently ignore the wrong one, which is
            # worse than rejecting it: sending 2.5's numeric budget to a 3.x model leaves
            # reasoning at its default with no error to notice, and it then consumed the whole
            # output allowance and returned MAX_TOKENS having written nothing.
            chosen = self._effective_thinking(thinking)
            if self.model.startswith("gemini-3"):
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_level={
                    "minimal": types.ThinkingLevel.MINIMAL, "low": types.ThinkingLevel.LOW,
                    "medium": types.ThinkingLevel.MEDIUM, "high": types.ThinkingLevel.HIGH,
                }[chosen])
            else:
                kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget={
                    "minimal": 0, "low": 2_048, "medium": self.THINKING_BUDGET,
                    "high": self.THINKING_BUDGET * 4,
                }[chosen])
            return types.GenerateContentConfig(**kwargs)

        # Adaptive retry: each attempt reacts to WHY the last one failed rather than blindly
        # resampling. Measured on a real prompt, a healthy call spends ~7k of the 65k allowance
        # on reasoning - but occasionally the model loops instead of converging and burns the
        # entire budget, returning MAX_TOKENS with nothing written. That is transient, so one
        # retry with reasoning cut to MINIMAL reliably gets an answer out; refusing to retry
        # means a run dies on a coin flip.
        temperature, thinking, transient_retries, response = self.TEMPERATURE, "normal", 0, None
        for attempt in range(3 + self.MAX_TRANSIENT_RETRIES):
            self.last_temperature = temperature
            self.last_thinking = self._effective_thinking(thinking)
            self.last_retried = attempt > 0
            try:
                response = client.models.generate_content(
                    model=self.model, contents=prompt, config=_config(temperature, thinking),
                )
            except errors.APIError as exc:
                # A model that REJECTS the thinking knob (rather than ignoring it) must still
                # work - `--model` is meant to accept anything the vendor offers.
                if "thinking" in str(exc).lower() and thinking != "off":
                    thinking = "off"
                    continue
                if _is_transient(exc) and transient_retries < self.MAX_TRANSIENT_RETRIES:
                    delay = self.TRANSIENT_BACKOFF_SECONDS * (2 ** transient_retries)
                    transient_retries += 1
                    print(f"note: provider temporarily unavailable (attempt "
                          f"{transient_retries}/{self.MAX_TRANSIENT_RETRIES}); retrying in {delay}s.")
                    time.sleep(delay)
                    continue
                raise RuntimeError(_gemini_api_error(exc, self.model)) from exc

            self.last_usage = _usage_summary(response)
            candidates = getattr(response, "candidates", None) or []
            finish_reason = getattr(candidates[0], "finish_reason", None) if candidates else None
            name = ((getattr(finish_reason, "name", None)
                     or str(finish_reason).rsplit(".", 1)[-1]).upper()
                    if finish_reason is not None else "")

            if name == "MAX_TOKENS" and thinking != "minimal":
                # Runaway reasoning, not an oversized answer - the visible output is usually
                # empty. Thinking and the answer draw on the SAME max_output_tokens allowance,
                # so the fix is to give reasoning less of it.
                #
                # Step DOWN one level rather than jumping straight to minimal. Cutting reasoning
                # to nothing does reliably produce *an* answer, but on a heavy-reasoning model
                # it produced a Go patch that did not compile - graded REGRESSION, from a model
                # that had been told not to think. One rung down still leaves real reasoning
                # budget, and there is another rung after it if that also overflows.
                step_down = {"high": "medium", "medium": "low", "low": "minimal"}
                current = self._effective_thinking(thinking)
                nxt = step_down.get(current, "minimal")
                print(f"note: the model exhausted its {self.MAX_OUTPUT_TOKENS}-token budget "
                      f"while reasoning ({self.last_usage}); retrying with {nxt} thinking.")
                if nxt == "minimal":
                    thinking = "minimal"
                else:
                    # Lower the level the config builder resolves to, keeping the retry state
                    # at "normal" so a later rung can step down again.
                    self.THINKING_OVERRIDE = nxt
                continue
            if name == "RECITATION" and temperature == self.TEMPERATURE:
                print("note: response blocked as recitation; retrying at a higher temperature.")
                temperature = self.RECITATION_RETRY_TEMPERATURE
                continue

            check_gemini_finish_reason(finish_reason, self.model)
            return response.text or ""

        candidates = getattr(response, "candidates", None) or []
        check_gemini_finish_reason(
            getattr(candidates[0], "finish_reason", None) if candidates else None, self.model)
        return (response.text if response is not None else "") or ""


PROVIDERS: dict[str, type] = {
    "bedrock": BedrockProvider,
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
}


def get_provider(name: str, model: Optional[str] = None) -> LLMProvider:
    provider_cls = PROVIDERS.get(name)
    if provider_cls is None:
        raise ValueError(
            f"unknown LLM provider: {name!r} (expected one of: {', '.join(sorted(PROVIDERS))})"
        )
    return provider_cls(model=model)


# ---------------------------------------------------------------------------
# LLM solver: provider-agnostic prompt building + diff extraction
# ---------------------------------------------------------------------------

# WHOLE FILES ONLY - never truncate.
#
# This is the load-bearing rule here, learned from a real failure. ansible-vars-001 correctly
# identified and ranked `lib/ansible/vars/manager.py` first, then silently cut it at 20,000
# bytes. The code needing the fix lives at line 786, far past the cut, so the model was asked
# to repair code it could not see - and nothing told it the file was partial. It filled the
# gap from training memory and produced a diff whose context matched no real line.
#
# The asymmetry that matters: a file that is ABSENT is honest - the model cannot use what it
# was not given, and can say so. A file that is TRUNCATED is a trap - it looks complete, so
# guessing looks reasonable. So a file is either included in full or not at all, and anything
# left out is named explicitly below so the model knows to stop rather than invent.
#
# The per-file byte cap this replaces was inherited from an era of 8K-32K context windows,
# where whole files genuinely did not fit. Gemini 2.5 Flash has a 1M-token window; the budget
# below is roughly 150K tokens, comfortably inside it. The cap had stopped being a real
# constraint long before it started causing this bug.
MAX_TOTAL_BYTES = 600_000
# A single file larger than this is excluded rather than truncated (see above). Generous
# enough that real source files pass; a guard against a vendored bundle or generated blob
# swallowing the whole budget.
MAX_SINGLE_FILE_BYTES = 400_000
MAX_LISTED_FILES = 1_000
# How many relevance-ranked files to fetch in the single batched read.
MAX_CANDIDATE_FILES = 300
# Hard cap on how many files get inlined. A large repo can fill the budget with ~150 files,
# which buries the relevant one in noise and invites edits to files that merely happened to be
# visible (observed: a 67-file cosmetic refactor that never touched the bug). Ranking already
# puts the named file first, so a tight cap costs little and keeps attention where it belongs.
MAX_INLINED_FILES = 60


# Identifiers a description names in backticks - `parseInstalledPackagesLine`, `combine_vars`.
# Backticks are the high-signal, low-noise source: SWE-bench Pro descriptions consistently mark
# up symbol names that way, so this needs no NLP and produces almost no false positives.
_BACKTICK_IDENTIFIER_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]{3,})`")
_SYMBOL_STOPWORDS = frozenset({
    "none", "true", "false", "null", "self", "this", "return", "class", "import",
    "dict", "list", "str", "int", "bool", "float", "string", "error", "bytes", "type",
})


def symbols_in_description(description: str) -> list[str]:
    """Backtick-quoted identifiers from a description, for locating files by CONTENT.

    Most descriptions name functions and classes but no file path at all - vuls-redhat-001
    names `parseInstalledPackagesLine` and never mentions scanner/redhatbase.go - so path and
    filename ranking has nothing to work with and the file that must change is never shown.
    Order is preserved and duplicates dropped so the resulting grep is deterministic.
    """
    seen: dict[str, None] = {}
    for match in _BACKTICK_IDENTIFIER_RE.finditer(description):
        if match.group(1).lower() not in _SYMBOL_STOPWORDS:
            seen.setdefault(match.group(1), None)
    return list(seen)


class LLMSolver:
    """Asks an LLM to produce a unified diff fixing the described bug.

    After solve() returns, ``self.metadata`` holds provider/model/latency/size
    info that the runner records in the report and DB for observability.
    """

    def __init__(
        self, provider: str = "gemini", model: Optional[str] = None,
        temperature: Optional[float] = None, thinking: Optional[str] = None,
        append_prompt: Optional[str] = None,
    ) -> None:
        self.provider_name = provider
        self.provider = get_provider(provider, model=model)
        # Pushed onto the provider so each keeps a single source of truth for its own limits,
        # rather than the solver second-guessing per-vendor knobs.
        if temperature is not None:
            self.provider.TEMPERATURE = temperature          # type: ignore[attr-defined]
        if thinking is not None:
            self.provider.THINKING_OVERRIDE = thinking       # type: ignore[attr-defined]
        # Appended to the prompt, never substituted for any of it - see _read_append_prompt.
        self.append_prompt = append_prompt
        self.metadata: dict[str, Any] = {}
        self._files_inlined = 0

    def _provider_state(self) -> dict[str, Any]:
        """Knobs and counters the provider recorded - the answer to "what varied between
        these two runs?". Read defensively: only the Gemini provider tracks all of them."""
        return {
            "temperature": getattr(self.provider, "last_temperature", None),
            "thinking": getattr(self.provider, "last_thinking", None),
            "token_usage": getattr(self.provider, "last_usage", {}),
            "provider_retried": getattr(self.provider, "last_retried", False),
        }

    def solve(self, files: FileReader, description: str) -> str:
        prompt = self._build_prompt(files, description)

        # Populated BEFORE the call rather than after it returns. The provider raises on
        # rejected credentials, 429, 503, and an exhausted output budget; while this dict was
        # only assigned afterwards, every one of those left it empty. The runner's failure path
        # reads this same dict (see SolveFailure), so `--keep-artifacts` created the run's
        # directory and then wrote nothing into it - discarding the one piece of evidence that
        # certainly exists at that point, and the only one that is expensive to reconstruct.
        self.metadata = {
            "provider": self.provider_name,
            "model": self.provider.model,
            "prompt_chars": len(prompt),
            "prompt_tokens_estimate": len(prompt) // 4,
            "files_inlined": self._files_inlined,
            "symbol_matched_files": getattr(self, "_symbol_matches", 0),
            # Bulky; the runner moves this (and raw_response) into the run's artifacts dir
            # and drops them from the report/DB row.
            "prompt": prompt,
        }

        start = time.monotonic()
        try:
            raw_response = self.provider.complete(prompt)
        finally:
            # Keep what the provider managed to record even when it raised: an exhausted-budget
            # failure sets last_usage on the way out, and those token counts ARE the diagnosis.
            self.metadata.update(self._provider_state())
        latency_s = time.monotonic() - start

        # Stored before _extract_diff, which rejects a response containing no usable diff -
        # precisely the case where seeing what the model actually said is the whole point.
        self.metadata["raw_response"] = raw_response
        diff = self._extract_diff(raw_response)
        self.metadata.update({
            "latency_seconds": round(latency_s, 2),
            "response_chars": len(raw_response),
        })
        return diff

    def _build_prompt(self, files: FileReader, description: str) -> str:
        file_list = files.list_files()
        parts = [
            "You are a software engineer making a change to a repository.",
            # "change"/"task", never "bug". SWE-bench Pro explicitly includes feature additions
            # and refactors alongside bug fixes, and its own issue_specificity taxonomy
            # (edge_case_bug, compatibility_bug, ...) presumes non-bug types exist. Telling a
            # model to "fix the bug" on a feature request misframes the work before it has read
            # anything.
            "Implement the change described below and return it as a minimal unified diff.",
            "",
            # Anti-memorization. Popular open-source repos are in every model's training data,
            # and the checked-out commit is deliberately an OLD one - the state before the fix.
            # Left unaddressed the model writes plausible code for the version it remembers,
            # producing a diff whose context lines don't match this commit, so it fails to
            # apply. Observed exactly this on ansible-vars-001 before the file was inlined.
            "CRITICAL: This repository is checked out at a specific pinned commit that almost "
            "certainly differs from whatever version of this project appears in your training "
            "data. Do NOT write code from memory. Every context line in your diff must match, "
            "character for character, the file contents given below under '## File contents'. "
            "If a file you need is not shown there, do not guess at its contents.",
            "",
            # Chain-of-extraction: force a verbatim copy of the target lines BEFORE writing the
            # diff. Negative instructions ("do not use memory") demonstrably failed on their
            # own; requiring the model to first reproduce the exact characters it was given
            # anchors attention on the provided text, and makes a memory-sourced line obvious
            # (it will not appear in the quoted block). The quote is stripped before applying.
            "BEFORE the diff, output a <quote> block containing the exact lines you are about "
            "to change, copied character-for-character from '## File contents' above, with a "
            "few lines of surrounding context. If you cannot find the lines you intend to "
            "change in the provided contents, say so in the quote block and stop - do not "
            "invent them. Then output the diff after a </quote> line.",
            "",
            "FORMAT:",
            "- After the quote block, output ONLY the diff text: no explanation, no commentary, "
            "no markdown code fences.",
            "- Use standard `git diff` format with a/ and b/ path prefixes "
            "(e.g. `--- a/pkg/mod.py` / `+++ b/pkg/mod.py`), paths relative to the repo root.",
            "- Every line inside a hunk must begin with a space (unchanged), '+' (added), or "
            "'-' (removed). Unchanged lines need their leading space too - do not paste them "
            "bare.",
            "- Context lines must match the file contents exactly, including blank lines "
            "(a blank context line is a single space character).",
            # Aider's advice here is to avoid "brittle specifiers like line numbers or line
            # counts" - but that works for them because their applier does fuzzy search/replace.
            # Ours is `git apply`, which is exact. Relaxing the prompt without relaxing the
            # applier was half a change, and the counting requirement turns out to carry a
            # second, load-bearing effect: a model that must count the lines in its hunk has to
            # LOOK at every line, including the blank ones it otherwise drops. The leniency now
            # lives in Runner._apply_diff, where it belongs, and the demand stays here.
            "- In each `@@ -old,N +new,M @@` header, N must equal the number of context plus "
            "removed lines in that hunk, and M the number of context plus added lines. Count "
            "them; a blank line is still a line.",
            "",
            # Scope is bounded by NECESSITY, not by a file count. Many files are inlined below
            # so the model has context to reason with, NOT as an invitation to edit them: one
            # run returned a 67-file cosmetic refactor that never touched the relevant code.
            # But "almost always one file" was equally wrong - the ansible golden patch spans
            # three - and understating scope produces the worse failure: a change that applies
            # cleanly, passes some tests, and is silently incomplete.
            # Kept short on purpose. Google's own Gemini 3 guidance is that it "responds best
            # to direct, clear instructions and may over-analyze verbose or overly complex
            # prompt engineering techniques used for older models" - so the justifications that
            # belong in code comments were moved out of the prompt and into these comments.
            "SCOPE:",
            "- Implement ONLY what is described - but implement ALL of it. Change every file "
            "the task genuinely requires: sometimes one, sometimes several.",
            "- Files under '## File contents' are CONTEXT to read. Do not edit one just "
            "because you can see it.",
            "- No cosmetic changes: no reformatting, lint pragmas, import reordering, "
            "unrelated refactoring, or rewording existing messages and comments.",
            # Phrased as reassurance rather than prohibition, which is how SWE-agent's own
            # instance template puts it ("I've already taken care of all changes to any of the
            # test files ... you DON'T have to modify the testing logic or any of the tests in
            # any way!"). A bare "do not modify tests" leaves the model a motive - it may still
            # think it needs to write one to check its work. Telling it the tests already exist
            # removes the motive, and here it is simply true: the graded tests are restored into
            # the repo afterwards, so anything the model writes is discarded.
            "- The tests for this change have ALREADY been written and are handled separately. "
            "You do not need to write, modify or fix any test - only the non-test source.",
            # Not a style preference - a correctness one. A model renamed two local variables
            # for readability, updated the declarations and missed the `if` guards three lines
            # below each, and the package stopped compiling ("undefined: relIndex"). No test can
            # run against a patch that does not build, so the whole attempt scored REGRESSION on
            # a change that was never needed in the first place.
            "- Do NOT rename existing variables, functions or fields.",
            "",
            # SWE-bench Pro's task text is not a raw GitHub issue. Scale's annotators rewrite
            # each one into a problem statement plus a human-authored Requirements list holding
            # "expected behavior by the implemented solution that will be explicitly tested for",
            # and an optional Interface section giving signatures and file paths for new or
            # changed public API - added specifically to "mitigate false negatives for unit test
            # verification". Those two sections are therefore the closest thing to the grading
            # criteria that exists outside the hidden tests, and saying so costs nothing and
            # leaks nothing: the model is being pointed at text it has already been given.
            "## Task description",
            "Note: if this description has a 'Requirements' section, each item in it is a "
            "behaviour that will be explicitly tested - treat it as the acceptance criteria "
            "and satisfy every one. If it has an 'Interface' section, use exactly the names, "
            "signatures and file paths given there; the tests refer to them by those names.",
            "",
            description.strip(),
            "",
            "## Repository files",
            ", ".join(file_list[:MAX_LISTED_FILES])
            + (f", ... ({len(file_list) - MAX_LISTED_FILES} more)" if len(file_list) > MAX_LISTED_FILES else ""),
            "",
            "## File contents",
        ]
        # On large repos not everything fits, so files are ranked and inlined until the byte
        # budget runs out. Three tiers, because "the description names this exact path" is far
        # stronger evidence than "some file happens to share this basename":
        #
        #   0  the description contains this file's full repo-relative path verbatim. SWE-bench
        #      Pro's `interface` field spells these out ("Location: `lib/ansible/vars/manager.py`"),
        #      so this is usually the file that must be fixed.
        #   1  only the basename/stem appears. Real but weak - it collides across a repo, e.g.
        #      a dozen unrelated `manager.py` files.
        #   2  everything else.
        #
        # This tiering exists because of a real failure: on ansible-vars-001 the correct file
        # ranked equal-tier with a large, unrelated `config/manager.py` that merely shared a
        # basename, and the budget was exhausted before the correct file's turn came. Exact-path
        # matches now sort strictly ahead, so a weak coincidental match can never crowd out the
        # file the task explicitly named.
        desc_lower = description.lower()

        # Tier 1 evidence: the file actually CONTAINS a symbol the description names, found by
        # grepping inside the container. Most descriptions name functions and classes but no
        # file path at all - vuls-redhat-001 names `parseInstalledPackagesLine` and never
        # mentions scanner/redhatbase.go - so path and basename ranking had nothing to work
        # with and the file that needed changing was never shown to the model.
        symbol_matches: set[str] = set()
        symbols = symbols_in_description(description)
        grep = getattr(files, "grep_files", None)
        if symbols and callable(grep):
            try:
                symbol_matches = set(grep(symbols))
            except Exception:
                # Ranking must degrade, never fail: a grep that errors just means this signal
                # is unavailable, and path/basename ranking still applies.
                symbol_matches = set()
        self._symbol_matches = len(symbol_matches)

        def relevance(rel: str) -> int:
            if rel.lower() in desc_lower:
                return 0                      # description names this exact path
            if rel in symbol_matches:
                return 1                      # file contains a symbol the description names
            name = rel.rsplit("/", 1)[-1]
            stem = name.rsplit(".", 1)[0]
            if name.lower() in desc_lower or (len(stem) > 3 and stem.lower() in desc_lower):
                return 2                      # only the basename matches - weak, collides a lot
            if _NON_CODE_RE.match(name) or "/vendor/" in f"/{rel}" or "/node_modules/" in f"/{rel}":
                return 4                      # prose and vendored trees - last even in fallback
            return 3

        ranked = [(relevance(rel), rel) for rel in file_list]
        # Tier 3 is "nothing about this file matches the task" - and when a stronger signal
        # exists, including it is not neutral padding, it is what pushes the signal out.
        # Measured on vuls-redhat-001: tier 1 identified the one correct file exactly, and the
        # 209 tier-3 files were then inlined around it until the budget ran out. Because ties
        # break alphabetically, the winners were CHANGELOG.md (52KB) and LICENSE (35KB) - a
        # quarter of the prompt was a changelog and a software licence, while the file that
        # had to change was 9.6% of it.
        #
        # Dropping tier 3 is safe, and that was checked rather than assumed: across both real
        # SWE-bench Pro bundles, every code file in the golden patch lands in tier 0, 1 or 2
        # (ansible needs lib/ansible/vars/manager.py at tier 0 and lib/ansible/utils/vars.py at
        # tier 2), and no golden file is tier 3. Tier 2 is deliberately NOT capped by count for
        # the same reason: ansible's tier 2 holds 45 files and the one it needs sorts mid-list,
        # so a top-N cut would remove exactly the file that matters.
        #
        # Only when tiers 0 and 1 are BOTH empty - a description naming no path and no symbol
        # we can find - does tier 3 come back, because then it is all there is.
        has_strong_signal = any(tier <= 1 for tier, _ in ranked)
        if has_strong_signal:
            ranked = [(tier, rel) for tier, rel in ranked if tier < 3]
        ordered = [rel for _, rel in sorted(ranked)]
        # One batched read of the top candidates rather than a round-trip per file; the
        # per-file caps and byte budget below decide what actually makes it into the prompt.
        # Only read what could plausibly be inlined. Reading one byte past the single-file
        # limit is what makes "was this file too big?" answerable at all - `head -c N` alone
        # returns exactly N bytes for both an N-byte file and a 10MB one.
        candidates = ordered[: min(MAX_CANDIDATE_FILES, MAX_INLINED_FILES * 3)]
        contents = files.read_many(candidates, max_bytes=MAX_SINGLE_FILE_BYTES + 1)

        total = 0
        inlined = 0
        skipped: list[str] = []
        for rel_path in candidates:
            content = contents.get(rel_path)
            if not content:
                continue
            if inlined >= MAX_INLINED_FILES or len(content) > MAX_SINGLE_FILE_BYTES \
                    or total + len(content) > MAX_TOTAL_BYTES:
                # Excluded whole rather than trimmed - see the MAX_TOTAL_BYTES comment. Named
                # below so the model knows this file exists but was not shown, instead of
                # silently receiving a partial file and filling the gap from memory.
                skipped.append(rel_path)
                continue
            total += len(content)
            inlined += 1
            parts.append(f"### {rel_path}\n```\n{content}\n```")

        if skipped:
            parts += [
                "",
                "## Files NOT shown",
                "These exist in the repo but their contents were not included. You have not "
                "seen them. If your fix depends on any of them, say so instead of guessing at "
                "their contents:",
                ", ".join(skipped),
            ]
        # A closing checklist, deliberately placed AFTER the file contents. Everything above it
        # sits ~86,000 tokens before the point where generation starts, and the rules that were
        # violated in practice were all present up there and read past. These four are not new
        # advice; they are the three observed failure modes on this bundle plus the one that
        # produced a silently incomplete patch, restated where the model is about to act.
        parts += [
            "",
            "## Before you answer",
            "Verify each point against the diff you are about to write.",
            # gemini-3.1-flash-lite: description stated Version "1:9-123a", model produced
            # "1:1:9-123a" - it prefixed the epoch in the helper while quoting, in its own
            # response, the caller that already prefixed it. It read the caller and never
            # evaluated it, so the instruction has to demand the arithmetic, not the reading.
            # Terse imperatives, no justifications. The reasoning behind each line lives in
            # these comments; Google's Gemini 3 guidance is that the model "favors directness
            # over persuasion and logic over verbosity" and over-analyses elaborate prompts.
            #
            # 1 covers two observed failures: the epoch applied twice (caller already did it),
            # and one field of the expected struct fixed while another broke.
            "1. Trace every worked example in the description through your changed code AND "
            "its callers. Compare EVERY field of the result, character by character, against "
            "the expected value.",
            # "Do not transform it again" alone is a principle with no trigger - it never told
            # the model WHEN to look. Measured across runs, the difference between a correct
            # and a wrong patch here is one line: whether the epoch, having been stripped off
            # the name, is then re-attached to the version that the caller twelve lines up
            # already prefixes. The final clause matters most: the description explicitly asks
            # for the epoch to be "included in the package version", so the task text and this
            # rule appear to conflict, and the model resolves that by adding it.
            "   Before you add a prefix, suffix or wrapper to a value you return, read the "
            "code that consumes it. If that code already adds the same thing, adding it in "
            "both places produces it twice - even when the description says the final result "
            "must contain it.",
            # Same bundle: the model called o.log.Warnf, which panics on the zero-value receiver
            # the code is constructed with in some paths, where the surrounding file appends to
            # an accumulator field instead. Both patterns were visible; it picked the one that
            # crashes.
            # A file usually offers more than one way to report a problem, so "match the
            # surrounding code" alone underdetermines it: the wrong candidate appeared 21 times
            # in this file and the right one 5, and the model took the common one, which panics
            # on the zero-value receiver the tests construct. The description said "append
            # warnings", so the task's own wording is the tiebreak. An earlier wording said only
            # "use the SAME mechanism", and the model added the correct call while KEEPING the
            # crashing one above it - adding is not replacing, so exclusivity is explicit.
            "2. Report problems using the mechanism the surrounding code already uses for that "
            "purpose, and only that one. If the description names it, use that name.",
            # gemini-2.5-flash renamed relIndex -> releaseIndex and left two references behind;
            # gemini-3-flash-preview left an `err` declared and unused. Both failed to compile,
            # so every test scored MISSING and the attempt scored zero. Scale's trajectory
            # analysis puts Syntax Error at 56.5% of Gemini 2.5 Pro's failed SWE-bench Pro
            # instances and 31.3% of Claude Opus 4.1's - the single largest failure class.
            "3. Read your diff as the compiler will. Every identifier exists. Every rename is "
            "complete. Every declared variable is used.",
            "4. Implement all of the described change, in every place the pattern occurs.",
            "5. Every context line matches the file contents above exactly.",
            "",
            # Negative constraints last, and the single most important restriction as the final
            # line: both come straight from Google's Gemini 3 prompting guidance for long
            # prompts, which also warns that the model "may over-analyze verbose or overly
            # complex prompt engineering techniques used for older models" - hence the terse
            # imperatives above, with every justification moved into these comments.
            #
            # This block replaces a rule that told the model its change should "also hold for
            # the awkward inputs around the ones described - malformed values, empty or missing
            # fields, and the boundaries of any range", adapted from SWE-agent's template. That
            # advice is safe for an AGENT, which can run the tests and see what it broke. In one
            # shot it is an invitation to speculate, and the model took it: it invented
            # robustness, rewrote a working RPM filename parser to "handle" a .src suffix, and
            # ate the dist tag - breaking three pass2pass tests. Measured on gemini-3.1-flash-
            # lite, that rule coincided with 0 regressions in 10 runs becoming 2 in 7. Reach was
            # bought with pass2pass, which is the wrong trade: the baseline invariant is the
            # thing this harness exists to protect.
            "Now remove anything you added beyond the description.",
            "",
            "The code you are changing already works for cases this task does not mention, and "
            "those cases are covered by tests that pass today. Breaking one is a regression, "
            "and a regression scores worse than an incomplete fix. Do not restructure, "
            "re-derive or harden logic that already works, and do not add handling for inputs "
            "the description does not raise. Change the fewest lines that satisfy it.",
        ]
        if getattr(self, "append_prompt", None):
            # Last, so it is the most salient thing in the prompt - and additive only. The task
            # description, the repo files and the output contract above are what make one run
            # comparable to another; a flag that could replace them would silently change what
            # is being measured while still looking like a benchmark result.
            parts += ["", "## Additional guidance", self.append_prompt.strip()]
        self._files_inlined = inlined
        return "\n".join(parts)

    @staticmethod
    def _extract_diff(text: str) -> str:
        text = text.strip()
        # Drop the chain-of-extraction <quote> block. It exists to anchor the model's attention
        # on the provided file text, not to be applied - and it deliberately contains lines that
        # look exactly like diff context, so it must go before any diff parsing happens.
        if "</quote>" in text:
            text = text.split("</quote>", 1)[1].strip()
        # Strip markdown fences if present despite instructions.
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        # Trim any prose before the first diff header ("Here's the fix: ...").
        # Match headers only at line starts so a mid-sentence "--- " can't truncate.
        if not text.startswith(("diff --git ", "--- ")):
            for marker in ("\ndiff --git ", "\n--- "):
                idx = text.find(marker)
                if idx != -1:
                    text = text[idx + 1:]
                    break
        text = LLMSolver._strip_wrapper_tags(text.strip())
        text = LLMSolver._repair_hunk_lines(text.strip())
        text = LLMSolver._repair_hunk_headers(text)
        return f"{text}\n" if text else ""

    @staticmethod
    def _strip_wrapper_tags(text: str) -> str:
        """Drop XML-ish wrapper tags a model puts around its answer (`<diff>` ... `</diff>`).

        Observed on a real run: the model emitted a correct two-hunk diff wrapped in
        `<diff>`/`</diff>`. The opening tag was removed by the prose trim above (it precedes the
        first `---` header), but the CLOSING tag survived at the end - inside the final hunk.
        Worse, `_repair_hunk_lines` then saw an unprefixed line in a hunk and helpfully turned
        it into a context line, so git looked for a literal `</diff>` in the source and the
        second hunk failed. A repair pass that does not know what it is repairing can make
        things worse, so this runs first.

        Only lines that are a bare tag at column ZERO are removed. Every real line inside a
        hunk carries a ' ', '+' or '-' prefix, so a genuine `</div>` in HTML is untouched -
        it would appear as ` </div>` or `+</div>`, neither of which matches.
        """
        return "\n".join(
            line for line in text.split("\n") if not _WRAPPER_TAG_RE.match(line)
        )

    @staticmethod
    def _repair_hunk_headers(text: str) -> str:
        """Recompute each `@@ -a,N +c,M @@` header's line counts from its own body.

        Observed on a real Gemini diff: a header claimed `-10,4 +10,4` over a body holding 6
        old and 5 new lines, and git rejected the whole patch as "corrupt patch at line 300".
        Models are reliably worse at counting lines than at writing them, so the body is
        treated as authoritative and the counts derived from it.

        Safe in both directions. If only the counts were wrong, this fixes the patch outright.
        If the body itself is wrong (a context line the model invented or dropped), the header
        becomes self-consistent and git fails on *content mismatch* instead - which is both a
        truer description of the problem and far easier to debug than "corrupt patch". What it
        never does is make a wrong patch apply: content still has to match the real file.
        """
        lines = text.split("\n")
        out: list[str] = []
        i = 0
        while i < len(lines):
            match = _HUNK_HEADER_RE.match(lines[i])
            if match is None:
                out.append(lines[i])
                i += 1
                continue

            body: list[str] = []
            j = i + 1
            while j < len(lines):
                if lines[j].startswith(("@@ ", "--- ", "+++ ", "diff --git ")):
                    break
                # A trailing "" from the final newline isn't part of the hunk.
                if j == len(lines) - 1 and lines[j] == "":
                    break
                body.append(lines[j])
                j += 1

            old_count = sum(1 for b in body if b[:1] in (" ", "-"))
            new_count = sum(1 for b in body if b[:1] in (" ", "+"))
            old_start, new_start, trailing = match.group(1), match.group(3), match.group(5)
            out.append(f"@@ -{old_start},{old_count} +{new_start},{new_count} @@{trailing}")
            out.extend(body)
            i = j
        return "\n".join(out)

    @staticmethod
    def _repair_hunk_lines(text: str) -> str:
        """Add back a missing leading space on unified-diff hunk context lines.

        Observed directly from a real Gemini response, not a hypothetical: the model got
        blank context lines right (a lone space) and +/- lines right, but emitted one
        non-blank context line with no leading space at all - it reads to a model like
        "just the code," so it produced exactly that. git rejects the *entire* hunk over
        this one line ("corrupt patch"), even though the fix is unambiguous: a line's
        position - inside a hunk, and not already +/-/\\ - already says what it must be.
        """
        lines = text.split("\n")
        out: list[str] = []
        in_hunk = False
        last = len(lines) - 1
        for i, line in enumerate(lines):
            # The final element from split("\n") on text ending in "\n" is "" and isn't a
            # real line - repairing it would inject a spurious trailing context line.
            trailing_artifact = i == last and line == ""
            if line.startswith("@@ "):
                in_hunk = True
            elif line.startswith(("diff --git ", "--- ", "+++ ")):
                in_hunk = False
            elif in_hunk and not trailing_artifact and not line.startswith((" ", "+", "-", "\\")):
                line = " " + line
            out.append(line)
        return "\n".join(out)


VALID_THINKING_LEVELS = ("minimal", "low", "medium", "high")


def get_solver(
    name: str,
    *,
    patch_text: str = "",
    provider: Optional[str] = None,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    thinking: Optional[str] = None,
    append_prompt: Optional[str] = None,
) -> Solver:
    if name == "stub":
        return StubSolver()
    if name == "oracle":
        return OracleSolver(patch_text)
    if name == "llm":
        resolved_provider = provider or os.environ.get("TASKCLI_PROVIDER") or "gemini"
        resolved_model = model or os.environ.get("TASKCLI_MODEL")
        if thinking is not None and thinking.lower() not in VALID_THINKING_LEVELS:
            raise ValueError(
                f"unknown --thinking level: {thinking!r} "
                f"(expected one of: {', '.join(VALID_THINKING_LEVELS)})"
            )
        return LLMSolver(
            provider=resolved_provider, model=resolved_model, temperature=temperature,
            thinking=thinking.lower() if thinking else None, append_prompt=append_prompt,
        )
    raise ValueError(f"unknown solver: {name!r} (expected one of: stub, oracle, llm)")

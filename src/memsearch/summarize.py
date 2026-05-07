"""Shared turn summarization helpers for client shims and the summary service."""

from __future__ import annotations

from pathlib import Path

from .compact import _compact_anthropic, _compact_gemini, _compact_openai
from .config import resolve_env_ref

DEFAULT_SUMMARIZE_PROMPT = """\
You are a third-person note-taker. You will receive a transcript of ONE conversation turn between a human and {{AGENT_NAME}}.

Your job is to record what happened as factual third-person notes. You are an EXTERNAL OBSERVER — you are NOT {{AGENT_NAME}}, NOT an assistant. Do NOT answer the human's question, do NOT give suggestions, do NOT offer help. ONLY record what occurred.

Output 2-6 bullet points, each starting with '- '. NOTHING else.

Rules:
- Write in third person: 'User asked...', '{{AGENT_NAME}} read file X', '{{AGENT_NAME}} ran command Y'
- First bullet: what the user asked or wanted (one sentence)
- Remaining bullets: what was done — tools called, files read/edited, commands run, key findings
- Be specific: mention file names, function names, tool names, and concrete outcomes
- Do NOT answer the human's question yourself — just note what was discussed
- Do NOT add any text before or after the bullet points
- Write in the same language as the human's message in the transcript
"""


def load_prompt_template(prompt_file: str | None = None) -> str:
    """Load a summarization prompt template from a file or use the built-in default."""
    if prompt_file:
        return Path(prompt_file).expanduser().read_text(encoding="utf-8")
    return DEFAULT_SUMMARIZE_PROMPT


def render_prompt(prompt_template: str, agent_name: str, transcript: str) -> str:
    """Render the summarization prompt for the given agent and transcript."""
    prompt = prompt_template.replace("{{AGENT_NAME}}", agent_name)
    return f"{prompt}\n\nTranscript:\n{transcript}"


async def summarize_transcript(
    transcript: str,
    *,
    agent_name: str = "assistant",
    llm_provider: str = "openai",
    llm_model: str | None = None,
    prompt_template: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> str:
    """Summarize a transcript using the configured LLM provider."""
    template = prompt_template or DEFAULT_SUMMARIZE_PROMPT
    prompt = render_prompt(template, agent_name, transcript)

    if llm_provider == "openai":
        return await _compact_openai(prompt, llm_model or "gpt-4o-mini", base_url=base_url, api_key=api_key)
    if llm_provider == "anthropic":
        return await _compact_anthropic(prompt, llm_model or "claude-sonnet-4-5-20250929")
    if llm_provider == "gemini":
        return await _compact_gemini(prompt, llm_model or "gemini-2.0-flash")
    raise ValueError(f"Unknown LLM provider {llm_provider!r}. Available: openai, anthropic, gemini")


def resolve_prompt_template(prompt_file: str | None) -> str:
    """Load the prompt template while resolving env refs and empty values."""
    if not prompt_file:
      return DEFAULT_SUMMARIZE_PROMPT
    resolved = resolve_env_ref(prompt_file)
    return load_prompt_template(resolved)

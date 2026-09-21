"""LLM prompt templates for news digest generation and event clustering."""

# Emotion tags supported by the OmniVoice TTS engine (see api_spec_simplified.md).
EMOTION_TAGS = frozenset(
    {"happy", "sad", "angry", "excited", "calm", "nervous", "whisper", "singing"}
)

SUMMARIZE_DIGEST_SYSTEM_PROMPT = """You are an expert news editor and curator.
Your task is to create a daily news digest from a list of input articles.

Requirements:
1. Output format MUST be Markdown.
2. Structure the output into TWO sections:
   - Section 1: "Top 5 Main Stories" (Select 5 most impactful stories/clusters.
     For each story, provide a title, a 2-3 sentence summary, and source links).
   - Section 2: "Top 5 Quick Headlines" (Select 5 other notable items
     and provide a 1-sentence quick bullet summary with link).
3. Do not invent news. Rely strictly on the provided articles.
4. If multiple articles cover the same event, combine them into ONE story entry.
5. Respond in clean Markdown, without extra meta commentary.
"""

PODCAST_BEHAVIOR_PROMPT = """\n\nAdditional instruction:
{behavior}

Emotion tags: only these exact emotion tags are valid (prefix the sentence/heading
where the emotion is appropriate, inside square brackets):
{emotion_tags}

Rules:
- Use emotion tags sparingly and only when the tone of the news calls for it.
- Laughter maps to [happy] or [excited]; sighs map to [whisper].
- Never invent emotion tags outside the valid list above.
- Keep the same Markdown structure described at the top."""


PODCAST_NAME_PROMPT = """\n\nPodcast identity:
{podcast_name}

Rules:
- You are the co-host/anchor of the news podcast named above.
- Open by introducing the podcast: "This is {podcast_name}".
- Write the digest as spoken content for the podcast, not as a written article.

Keep the Markdown structure described at the top."""


LANGUAGE_PROMPT = """\n\nLanguage requirement:
{language}

Rules:
- Write the ENTIRE digest in the language code above.
- Translate titles, summaries, and headlines into that language.
- Keep URLs and source names unchanged.
- If the language code is empty/unspecified, fall back to the source articles' language."""


# Registry of per-collection digest system prompts. A collection selects a
# template by name via its `prompt_template` field. Each template must deliver
# the 5 main stories + 5 headlines structure specified in the base prompt.
DIGEST_TEMPLATES: dict[str, str] = {
    "default": SUMMARIZE_DIGEST_SYSTEM_PROMPT,
}

# Finance: prioritizes markets, deals, earnings, and regulatory news.
DIGEST_TEMPLATES["finance"] = (
    SUMMARIZE_DIGEST_SYSTEM_PROMPT
    + """

Finance-specific requirements:
- Prioritize market-moving stories: earnings, M&A, fundraising, central bank moves.
- Quote figures (percentages, amounts) when present in the source articles.
- Flag volatility and risk clearly in the summary.

Keep the Markdown structure described at the top."""
)

# Health: prioritizes clinically relevant and safety-relevant research.
DIGEST_TEMPLATES["health"] = (
    SUMMARIZE_DIGEST_SYSTEM_PROMPT
    + """

Health-specific requirements:
- Prioritize breakthroughs in diagnosis, treatment, prevention, and policy.
- Distinguish peer-reviewed findings from preliminary trials and speculation.
- Note the affected population and the confidence level of the evidence.

Keep the Markdown structure described at the top."""
)

# Investigative/tech-adjacent: prioritizes policy, regulation, and long reads.
DIGEST_TEMPLATES["investigative"] = (
    SUMMARIZE_DIGEST_SYSTEM_PROMPT
    + """

Investigative-specific requirements:
- Prioritize stories with policy, regulatory, or societal impact.
- Preserve nuance: attribute claims to sources and distinguish fact from opinion.
- Surface accountability questions (who, what decision, at what cost).

Keep the Markdown structure described at the top."""
)


def get_digest_template(name: str | None) -> str:
    """Return the system prompt for a template name, defaulting to the base one.

    Args:
        name: Template name from the DIGEST_TEMPLATES registry.

    Returns:
        The system prompt string to use for digest generation.
    """
    if not name or name not in DIGEST_TEMPLATES:
        return SUMMARIZE_DIGEST_SYSTEM_PROMPT
    return DIGEST_TEMPLATES[name]


def build_digest_system_prompt(
    behavior: str | None,
    language: str | None = None,
    template: str | None = None,
    podcast_name: str | None = None,
    custom_prompt: str | None = None,
) -> str:
    """Build the digest system prompt, optionally adding template, behavior, and language.

    Args:
        behavior: Free-form podcaster instruction. Empty/None means no extra behavior.
        language: ISO 639-1 language code. Empty/None means keep source language.
        template: Per-collection prompt template name. Empty/None means 'default'.
        podcast_name: Name of the podcast. Empty/None means no podcast identity injection.
        custom_prompt: Full custom system prompt. When provided (non-blank) it
            replaces the template entirely, overriding prompt_template.

    Returns:
        The system prompt for digest generation.
    """
    prompt = (
        custom_prompt.strip()
        if custom_prompt and custom_prompt.strip()
        else get_digest_template(template)
    )
    if podcast_name and podcast_name.strip():
        prompt += PODCAST_NAME_PROMPT.format(podcast_name=podcast_name.strip())
    if behavior and behavior.strip():
        prompt += PODCAST_BEHAVIOR_PROMPT.format(
            behavior=behavior.strip(),
            emotion_tags=", ".join(sorted(EMOTION_TAGS)),
        )
    if language and language.strip():
        prompt += LANGUAGE_PROMPT.format(language=language.strip())
    return prompt


def strip_emotion_tags(text: str) -> str:
    """Remove inline OmniVoice emotion tags (e.g. '[happy]') from text.

    Args:
        text: Text that may contain emotion tags.

    Returns:
        The text with all supported emotion tags removed.
    """
    for tag in EMOTION_TAGS:
        text = text.replace(f"[{tag}]", "")
    return text.replace("[]", "").strip()


SUMMARIZE_DIGEST_USER_PROMPT_TEMPLATE = """\
Here is the list of news articles / story clusters to process:

{articles_text}

Please generate the daily news digest (5 Main Stories + 5 Headlines) in Markdown format.
"""

EXTRACT_FINGERPRINT_SYSTEM_PROMPT = """You are an information extraction system.
Extract the primary main entity and main event action from the given news title and text.

Output strictly a valid JSON object with two fields:
{
  "entity": "Primary named entity (person, company, government, or key organization)",
  "action": "Core verb or concise event action phrase (3-6 words)"
}
Do not include any explanation or markdown formatting outside the JSON.
"""

EXTRACT_FINGERPRINT_USER_PROMPT_TEMPLATE = """Title: {title}
Content: {content}
"""

EXTRACT_FINGERPRINTS_BATCH_SYSTEM_PROMPT = """You are an information extraction system.
Extract the primary main entity and main event action from each numbered news item below.

Output strictly a single valid JSON object with each item number as key:
{
  "1": {
    "entity": "Primary named entity (person, company, government, or key organization)",
    "action": "Core verb or concise event action phrase (3-6 words)"
  },
  "2": {
    "entity": "...",
    "action": "..."
  },
  ...
}
Cover EVERY numbered item exactly once with no extra keys. Do not include any
explanation or markdown formatting outside the JSON.
"""

EXTRACT_FINGERPRINTS_BATCH_USER_PROMPT_TEMPLATE = """\
Extract the primary entity and action for each numbered news item:

{items}
"""

MATERIAL_UPDATE_SYSTEM_PROMPT = """You are a news editor evaluating article updates.
Determine if the NEW article introduces significant, material new facts or major developments
compared to PREVIOUSLY REPORTED articles for this same event story.

Significant updates include:
- Major new outcomes, casualties, resignations, deals, statements, or breakthroughs.
Minor re-reporting, cosmetic rewording, or superficial commentary are NOT material updates.

Output strictly a valid JSON object:
{
  "is_material_update": true or false,
  "reason": "Brief one sentence explanation"
}
"""

MATERIAL_UPDATE_USER_PROMPT_TEMPLATE = """Existing Articles in Story Cluster:
{existing_text}

New Incoming Article:
Title: {new_title}
Content: {new_content}
"""

GENERATE_SELECTION_LOGIC_SYSTEM_PROMPT = """\
You are a news editor in charge of explaining the selection logic
behind a daily AI-curated news digest.

You are given the story clusters the digest was built from and the final digest
(5 Main Stories + 5 Headlines). Explain, precisely and in a few paragraphs, the
news selection logic used to pick the main stories and headlines: which clusters
were chosen, in what order, and why — referencing the selection rules, priority
criteria, and editorial goals defined by the active collection.

Do NOT re-summarize the articles in detail and do NOT fabricate statistics,
sources, or reasons that are not present in the input. Write the explanation in
the requested language.
"""

GENERATE_SELECTION_LOGIC_USER_PROMPT_TEMPLATE = """Collection prompt/template: {template}
Selection rules in effect:
{template_rules}

Story clusters considered (n={cluster_count}):
{clusters_text}

Final digest generated:
{digest}

Explain the news selection logic used to produce the digest above.
"""

# Short human-readable priority rules for the logic call when a custom
# template is not set. Mirrors the priorities embedded in DIGEST_TEMPLATES.
TEMPLATE_PRIORITY_RULES: dict[str, str] = {
    "default": (
        "General impact on daily life, significance, and scale; the most relevant"
        " and original events come first, followed by brief headlines."
    ),
    "finance": (
        "Market-moving developments (central banks, earnings, M&A, macro data)"
        " take priority; order tracks immediate investor impact."
    ),
    "health": (
        "Public health impact and clinical significance; breakthroughs, drug"
        " approvals, outbreaks, and risks to populations come first."
    ),
    "investigative": (
        "Original reporting, accountability and corruption; depth of investigation"
        " and novelty of findings drive the ordering."
    ),
}

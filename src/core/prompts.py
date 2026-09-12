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


def build_digest_system_prompt(behavior: str | None) -> str:
    """Build the digest system prompt, optionally including a podcast behavior.

    Args:
        behavior: Free-form podcaster instruction. Empty/None means no extra behavior.

    Returns:
        The system prompt for digest generation.
    """
    if not behavior or not behavior.strip():
        return SUMMARIZE_DIGEST_SYSTEM_PROMPT
    return SUMMARIZE_DIGEST_SYSTEM_PROMPT + PODCAST_BEHAVIOR_PROMPT.format(
        behavior=behavior.strip(),
        emotion_tags=", ".join(sorted(EMOTION_TAGS)),
    )


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

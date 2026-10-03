"""LLM prompt templates for news digest generation and event clustering."""

# Emotion tags supported by the OmniVoice TTS engine (see api_spec_simplified.md).
EMOTION_TAGS = frozenset(
    {"happy", "sad", "angry", "excited", "calm", "nervous", "whisper", "singing"}
)


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


def _build_base_digest_prompt(main_story_count: int, headline_count: int) -> str:
    """Build the base digest system prompt with configurable story/headline counts.

    Args:
        main_story_count: Number of main stories to include.
        headline_count: Number of quick headlines to include.

    Returns:
        The base system prompt string with the counts interpolated.
    """
    return (
        "You are an expert news editor and curator.\n"
        "Your task is to create a daily news digest from a list of input articles.\n\n"
        "Requirements:\n"
        "1. Output format MUST be Markdown.\n"
        "2. Structure the output into TWO sections:\n"
        f'   - Section 1: "Top {main_story_count} Main Stories" '
        f"(Select {main_story_count} most impactful stories/clusters.\n"
        "     For each story, provide a title, a 2-3 sentence summary, and source links).\n"
        f'   - Section 2: "Top {headline_count} Quick Headlines" '
        f"(Select {headline_count} other notable items\n"
        "     and provide a 1-sentence quick bullet summary with link).\n"
        "3. Do not invent news. Rely strictly on the provided articles.\n"
        "4. If multiple articles cover the same event, combine them into ONE story entry.\n"
        "5. Respond in clean Markdown, without extra meta commentary."
    )


# Domain-specific additions appended to the base prompt.
DIGEST_TEMPLATE_ADDITIONS: dict[str, str] = {
    "default": "",
    "finance": """

Finance-specific requirements:
- Prioritize market-moving stories: earnings, M&A, fundraising, central bank moves.
- Quote figures (percentages, amounts) when present in the source articles.
- Flag volatility and risk clearly in the summary.

Keep the Markdown structure described at the top.""",
    "health": """

Health-specific requirements:
- Prioritize breakthroughs in diagnosis, treatment, prevention, and policy.
- Distinguish peer-reviewed findings from preliminary trials and speculation.
- Note the affected population and the confidence level of the evidence.

Keep the Markdown structure described at the top.""",
    "investigative": """

Investigative-specific requirements:
- Prioritize stories with policy, regulatory, or societal impact.
- Preserve nuance: attribute claims to sources and distinguish fact from opinion.
- Surface accountability questions (who, what decision, at what cost).

Keep the Markdown structure described at the top.""",
    "gaming": """

Gaming-specific requirements:
- Prioritize new releases, major updates, hardware announcements, esports results,
  and industry controversies.
- For main stories, use extended summaries of 5-6 sentences covering context,
  significance, and community reaction.
- Quote key figures (player counts, revenue, review scores) when present.
- Note platform exclusivity and release dates clearly.

Keep the Markdown structure described at the top.""",
    "ai": """

AI-specific requirements:
- Prioritize: new model releases, benchmark results with measurable gains,
  research papers with broad impact, product launches, funding rounds, and
  AI policy or regulation.
- Always name the actor (lab or company), the model or version, and the date.
- Separate measured results from marketing claims; call out when a
  "breakthrough" has no independent evaluation.
- Mark open-weight vs proprietary models, and note hardware or VRAM needs
  when the sources mention them.
- For main stories, use extended summaries of 5-6 sentences covering context,
  significance, and community reaction.
- Keep figures (benchmarks, percentages, dollar amounts, context lengths)
  from the sources.

Keep the Markdown structure described at the top.""",
}


def get_digest_template(
    name: str | None, main_story_count: int = 5, headline_count: int = 5
) -> str:
    """Return the system prompt for a template name with the given counts.

    Args:
        name: Template name from the DIGEST_TEMPLATE_ADDITIONS registry.
        main_story_count: Number of main stories to include.
        headline_count: Number of quick headlines to include.

    Returns:
        The system prompt string to use for digest generation.
    """
    if not name or name not in DIGEST_TEMPLATE_ADDITIONS:
        name = "default"
    base = _build_base_digest_prompt(main_story_count, headline_count)
    return base + DIGEST_TEMPLATE_ADDITIONS[name]


def build_digest_system_prompt(
    behavior: str | None,
    language: str | None = None,
    template: str | None = None,
    podcast_name: str | None = None,
    custom_prompt: str | None = None,
    main_story_count: int = 5,
    headline_count: int = 5,
) -> str:
    """Build the digest system prompt, optionally adding template, behavior, and language.

    Args:
        behavior: Free-form podcaster instruction. Empty/None means no extra behavior.
        language: ISO 639-1 language code. Empty/None means keep source language.
        template: Per-collection prompt template name. Empty/None means 'default'.
        podcast_name: Name of the podcast. Empty/None means no podcast identity injection.
        custom_prompt: Full custom system prompt. When provided (non-blank) it
            replaces the template entirely, overriding prompt_template, behavior,
            language, and podcast_name.
        main_story_count: Number of main stories to include (default 5).
        headline_count: Number of quick headlines to include (default 5).

    Returns:
        The system prompt for digest generation.
    """
    base_prompt = get_digest_template(template, main_story_count, headline_count)

    prompt = custom_prompt.strip() if custom_prompt and custom_prompt.strip() else base_prompt
    if custom_prompt and custom_prompt.strip():
        # Custom prompt completely replaces the template; don't add behavior,
        # language, or podcast_name.
        return prompt
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


SUMMARIZE_DIGEST_USER_PROMPT_TEMPLATE = (
    "Here is the list of news articles / story clusters to process:\n\n"
    "{articles_text}\n\n"
    "Please generate the daily news digest "
    "({main_story_count} Main Stories + {headline_count} Headlines) "
    "in Markdown format."
)


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

GENERATE_SELECTION_LOGIC_SYSTEM_PROMPT = (
    "You are a news editor in charge of explaining the selection logic\n"
    "behind a daily AI-curated news digest.\n\n"
    "You are given the story clusters the digest was built from and the final digest\n"
    "({main_story_count} Main Stories + {headline_count} Headlines). "
    "Explain, precisely and in a few paragraphs, the\n"
    "news selection logic used to pick the main stories and headlines: which clusters\n"
    "were chosen, in what order, and why — referencing the selection rules, priority\n"
    "criteria, and editorial goals defined by the active collection.\n\n"
    "Do NOT re-summarize the articles in detail and do NOT fabricate statistics,\n"
    "sources, or reasons that are not present in the input. Write the explanation in\n"
    "the requested language."
)

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
    "gaming": (
        "New releases, major patches, hardware reveals, esports results, and"
        " industry controversies take priority; order tracks player impact and"
        " community buzz."
    ),
}


# =====================================================================
# NARRATION SCRIPT GENERATION (Phase 7.5)
# =====================================================================

NARRATION_SCRIPT_SYSTEM_PROMPT = (
    "You are an expert news narrator and scriptwriter.\n"
    "Your task is to turn a news digest into a\n"
    "multi-speaker audio script for a narration cast.\n\n"
    "INPUT:\n"
    '- A markdown digest with "Top {main_story_count} Main Stories" '
    'and "Top {headline_count} Quick Headlines".\n'
    "- A NarrationSpec defining the cast (roles), default language, pause timing,\n"
    "  music bed, and render strategy (TURNS or CONVERSATIONS).\n\n"
    "OUTPUT:\n"
    "A JSON object matching the NarrationScript schema with these fields:\n"
    '- source: "llm" (this is always LLM-generated)\n'
    "- language: ISO 639-1 code from the spec\n"
    "- title: Program title from the spec\n"
    "- segments: Array of segment objects, each with:\n"
    "    - role: Role name from the spec.roles (must match exactly)\n"
    "    - text: Spoken text for this segment (clean, no markdown, no emotion tags)\n"
    "    - emotion: Optional emotion tag from EMOTION_TAGS (only if the spec role\n"
    "      allows it and the content calls for it)\n"
    "    - pause_ms: Optional override of the default pause (ms)\n\n"
    "RULES:\n"
    "1. The script must cover ALL main stories and ALL headlines present in the digest, in order.\n"
    "2. Distribute speaking time across the available roles as naturally as possible.\n"
    "   If only 1 role exists, that role speaks everything.\n"
    "3. For TURNS strategy: each segment is one turn. Aim for ~2-4 turns per story.\n"
    "4. For CONVERSATIONS strategy: write as a continuous dialogue with role\n"
    "   transitions; the renderer will send the whole conversation in one request.\n"
    "5. Emotion tags are ONLY allowed when the spec role permits them (OmniVoice\n"
    "   backend). Use sparingly: happy, sad, angry, excited, calm, nervous, whisper,\n"
    "   singing. If the spec role has a fixed emotion default, you may omit it.\n"
    "6. Text must be spoken-form: no markdown, no bullet points, no URLs, no\n"
    "   bracketed source citations. Convert to natural speech.\n"
    "7. Keep each segment's text under the spec's max_chars_per_request limit.\n"
    "8. The total script should render to roughly 8-14 minutes of audio.\n"
    "9. Output ONLY the JSON object. No markdown, no explanation, no extra keys."
)

NARRATION_SCRIPT_USER_PROMPT_TEMPLATE = """Digest to narrate (Markdown):
{digest_markdown}

Narration Spec (JSON):
{spec_json}

Generate the NarrationScript JSON now.
"""

# Registry of per-collection narration script prompts (extensible for Phase 7.5+)
NARRATION_SCRIPT_TEMPLATES: dict[str, str] = {
    "default": NARRATION_SCRIPT_SYSTEM_PROMPT,
    "gaming": (
        NARRATION_SCRIPT_SYSTEM_PROMPT
        + """

Gaming podcast style:
- Two hosts in conversational dialogue (Michael Scott as the enthusiastic host,
  Dwight as the deadpan analyst/commentator).
- Open with: "Welcome to Game Over, your daily gaming briefing."
- Alternate hosts naturally: Michael leads the main stories with excitement,
  Dwight provides context, skepticism, or industry insight.
- For headlines: quick back-and-forth banter, one-liners per item.
- Use gaming terminology naturally (patch, meta, nerf, speedrun, NPC, grind, etc.).
- Keep segments tight: 2-3 sentences max per turn.
- Total target: ~10-12 minutes for 5 extended stories + 10 headlines.
"""
    ),
    "ai": (
        NARRATION_SCRIPT_SYSTEM_PROMPT
        + """

AI podcast style:
- Two hosts in continuous dialogue: Michael (enthusiastic host, curious
  about the implications) and Dwight (deadpan analyst, skeptical, dry).
- Open with: "Welcome to AI Pulse, your daily AI briefing."
- Alternate naturally. Michael frames the story, Dwight challenges it.
- For headlines: quick back-and-forth, one line per item.
- Use AI terminology naturally (model, weights, inference, fine-tuning,
  tokens, context window, benchmark, open weights, GPU, latency).
- NEVER emit emotion tags or bracketed annotations: the conversations
  render strategy ignores them and fails if they appear.
- Keep segments tight: 2-3 sentences max per turn.
- Total target: ~10-12 minutes for 5 main stories + 8 headlines.
"""
    ),
}


# --- Phase 7.13: LLM output verification -------------------------------------
# Two prompts, both answered as JSON: the first decomposes the generated text
# into atomic checkable claims, the second judges a batch of those claims against
# the source articles. The prompts are deliberately strict about evidence: a
# small local model is prone to "helpfully" agreeing with a claim, so the judge
# is told that an unsupported claim must be reported as such and that quoting is
# mandatory (a verdict without a quote is treated as unsupported by the parser).

VERIFY_CLAIMS_SYSTEM_PROMPT = (
    "You are a fact-checking assistant. You receive a generated news digest and\n"
    "must decompose it into atomic, independently verifiable claims.\n\n"
    "Rules:\n"
    "- One claim per entry: a single fact, with its subject, quantity and time\n"
    "  when the text has them.\n"
    "- Split compound sentences into separate claims.\n"
    "- Keep every number, date, percentage and proper noun exactly as written.\n"
    "- Do NOT verify anything, do NOT add facts and do NOT comment.\n"
    "- Ignore pure opinion, framing, transitions and the headline section's\n"
    "  editorial connective tissue; keep only statements that could be wrong.\n\n"
    'Answer with JSON only: {{"claims": ["...", "..."]}}'
)

VERIFY_CLAIMS_USER_PROMPT_TEMPLATE = """Digest to decompose (Markdown):
{digest}

Return the atomic claims as a JSON object with a single "claims" array.
"""

VERIFY_CLAIM_SYSTEM_PROMPT = (
    "You are a strict fact-checking judge. You receive atomic claims taken from a\n"
    "generated news digest and the source articles the digest was built from.\n\n"
    "For every claim decide exactly one label:\n"
    "- 'supported': the sources state this fact.\n"
    "- 'contradicted': the sources state something incompatible with this claim.\n"
    "- 'unsupported': the sources neither state nor contradict it.\n\n"
    "Rules:\n"
    "- Judge only against the sources. Never use your own knowledge and never\n"
    "  assume a claim is true because it sounds plausible.\n"
    "- 'unsupported' is the correct answer when the evidence is thin, ambiguous,\n"
    "  or only about a different entity, place, date or unit.\n"
    "- A paraphrased but factually identical statement is 'supported'.\n"
    "- 'quote' must be copied verbatim from the sources and be short (max 25\n"
    "  words). Use an empty string when there is no such text.\n"
    "- Return one entry per claim, in the same order, with the same 'id'.\n\n"
    'Answer with JSON only: {{"verdicts": [{{"id": 1, "label": "supported", '
    '"quote": "..."}}]}}'
)

VERIFY_CLAIM_USER_PROMPT_TEMPLATE = """Source articles (n={source_count}):
{sources_text}

Claims to judge:
{claims_text}

Return a JSON object with a single "verdicts" array, one entry per claim id.
"""


def get_narration_script_template(name: str | None) -> str:
    """Return the system prompt for a narration script template name.

    Args:
        name: Template name from the NARRATION_SCRIPT_TEMPLATES registry.

    Returns:
        The system prompt string to use for narration script generation.
    """
    if not name or name not in NARRATION_SCRIPT_TEMPLATES:
        return NARRATION_SCRIPT_SYSTEM_PROMPT
    return NARRATION_SCRIPT_TEMPLATES[name]


def build_narration_script_system_prompt(template: str | None = None) -> str:
    """Build the narration script system prompt.

    Args:
        template: Template name from NARRATION_SCRIPT_TEMPLATES. Empty/None = default.

    Returns:
        The system prompt for narration script generation.
    """
    return get_narration_script_template(template)


# LANGUAGE TEACHER LESSON GENERATION (Phase 7.12)
LESSON_SYSTEM_PROMPT_TEMPLATE = """Eres un profesor de idiomas experimentado, claro y estructurado.
Te entrego unos apuntes de un alumno y debo convertirlos en una clase completa:
por un lado un documento de estudio en Markdown y, por otro, el guion hablado
que una voz (o dos) grabará en audio.

Modo de la clase: {mode}
Reparto de voces (roles): {roles_json}
Lengua objetivo: {target_language}
Lengua nativa del alumno: {native_language}

Instrucciones del modo:
{mode_instructions}

Devuelve SOLO un objeto JSON (sin markdown, sin ``` , sin comentarios) con
exactamente esta forma:
{{
  "title": "titulo corto y descriptivo de la clase",
  "markdown": "la clase completa para LEER, en Markdown, con secciones, ejemplos y ejercicios",
  "segments": [
    {{"role": "nombre_exacto_de_un_rol", "text": "lo que dice esa voz, sin Markdown"}},
    {{"role": "otro_rol", "text": "..."}}
  ]
}}

Reglas:
- "markdown" es el material de estudio del alumno: usa titulos, listas y
  ejemplos; incluye siempre al final una seccion de ejercicios con sus
  soluciones.
- "segments" es lo que se PRONUNCIA: 8 a 80 segmentos, cada uno entre 1 y 1500
  caracteres, sin Markdown (sin #, *, ni listas), en frases naturales para
  escuchar.
- El rol de cada segmento debe ser exactamente uno de los nombres de
  {roles_json}; respeta los idiomas asignados a cada rol.
- Los segmentos deben cubrir la clase entera (no la resumas) y son para ser
  escuchados: nada de "observa la imagen", "en la tabla anterior" ni
  referencias al documento.
- language del alumno = {native_language}; no inventes datos ni frases
  "oficiales" que no esten en los apuntes: puedes explicar, ejemplificar y
  practicar a partir de ellos.
"""

LESSON_MODE_INSTRUCTIONS = {
    "mixed": (
        "Clasica mixta: una unica voz explica en {native_language} e intercala "
        "vocabulario, ejemplos y frases de muestra en {target_language}, "
        "traduciendo siempre lo importante. El alumno puede seguirla aunque su "
        "nivel en la lengua objetivo sea bajo."
    ),
    "dual": (
        "Dual (dos voces): el PRIMER rol explica y orienta en {native_language}; "
        "el SEGUNDO rol da las explicaciones y los ejemplos hablados en "
        "{target_language}. Alternan por bloques con naturalidad y se citan "
        "el uno al otro."
    ),
    "native": (
        "Explicacion en la lengua nativa: TODO se explica en {native_language} y "
        "las frases o vocabulario en {target_language} aparecen citadas como "
        "material a estudiar, con su traduccion."
    ),
}

LESSON_USER_PROMPT_TEMPLATE = """Apuntes del alumno:
--- INICIO APUNTES ---
{notes}
--- FIN APUNTES ---

Genera ahora la clase en JSON."""


def build_lesson_system_prompt(
    mode: str,
    roles_json: str,
    target_language: str,
    native_language: str,
) -> str:
    """Build the system prompt for lesson generation (Phase 7.12).

    Args:
        mode: Lesson mode: 'mixed', 'dual' or 'native'.
        roles_json: JSON description of the cast (names + languages).
        target_language: ISO 639-1 language the student is learning.
        native_language: ISO 639-1 language the student thinks in.

    Returns:
        The formatted system prompt.

    Raises:
        KeyError: If the mode has no instruction text (caller validates first).
    """
    instructions = LESSON_MODE_INSTRUCTIONS[mode].format(
        native_language=native_language,
        target_language=target_language,
    )
    return LESSON_SYSTEM_PROMPT_TEMPLATE.format(
        mode=mode,
        roles_json=roles_json,
        target_language=target_language,
        native_language=native_language,
        mode_instructions=instructions,
    )

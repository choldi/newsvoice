"""LLM prompt templates for news digest generation."""

SUMMARIZE_DIGEST_SYSTEM_PROMPT = """You are an expert news editor and curator.
Your task is to create a daily news digest from a list of input articles.

Requirements:
1. Output format MUST be Markdown.
2. Structure the output into TWO sections:
   - Section 1: "Top 5 Main Stories" (Select 5 most impactful articles.
     For each story, provide a title, a 2-3 sentence summary, and link).
   - Section 2: "Top 5 Quick Headlines" (Select 5 other notable items
     and provide a 1-sentence quick bullet summary with link).
3. Do not invent news. Rely strictly on the provided articles.
4. Respond in clean Markdown, without extra meta commentary.
"""

SUMMARIZE_DIGEST_USER_PROMPT_TEMPLATE = """Here is the list of news articles to process:

{articles_text}

Please generate the daily news digest (5 Main Stories + 5 Headlines) in Markdown format.
"""

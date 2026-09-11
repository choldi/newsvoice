"""Temporal workflow definitions for the news-engine pipeline.

Workflows orchestrate the daily digest pipeline. They must be deterministic:
no network I/O, no randomness, no external process calls. All side effects
live in activities.
"""

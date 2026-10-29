"""Shared language requirements for model reasoning and generated prose."""

from core.prompts import render_prompt



def normalize_language(value: str) -> str:
    language = value.strip()
    if not language:
        raise ValueError("Language must not be empty.")
    return language


def language_instructions(language: str) -> str:
    language = normalize_language(language)
    if language.casefold().replace("_", "-") in {
        "zh", "zh-cn", "zh-hans", "chinese", "中文", "简体中文", "简体中文（中国）",
    }:
        return render_prompt("language/zh_cn.txt")
    return render_prompt("language/default.txt", language=language)

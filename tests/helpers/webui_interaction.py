"""Helpers of tests/test_webui_interaction.py that other test modules use too."""

from typing import Any


class Browser:
    """Answers each question from a script, and keeps what it was asked."""

    def __init__(self, answers: list[Any]) -> None:
        self.answers = list(answers)
        self.questions: list[dict[str, Any]] = []

    async def ask(self, question: dict[str, Any]) -> Any:
        self.questions.append(question)
        return self.answers.pop(0)

    def show_spectrals(self, folder: str, files: list[str]) -> None:
        pass

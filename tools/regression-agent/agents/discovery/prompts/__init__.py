from pathlib import Path

SYSTEM_PROMPT_PATH = Path(__file__).resolve().parent / "system_prompt.md"
CONTRACTS_PROMPT_PATH = Path(__file__).resolve().parent / "contracts_prompt.md"

__all__ = ["CONTRACTS_PROMPT_PATH", "SYSTEM_PROMPT_PATH"]

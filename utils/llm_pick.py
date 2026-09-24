"""Configurable OpenAI models; verify model access in the deployment account."""

import os
from langchain_openai import ChatOpenAI


def pick_llm(level: str):
    key = {"low": "MODEL_FAST", "medium": "MODEL_SQL", "high": "MODEL_ETL",
           "etl": "MODEL_ETL", "claude": "MODEL_ETL"}.get(level.lower())
    if key is None:
        raise ValueError(f"Unsupported level: {level}")
    model = os.environ.get(key, "gpt-4.1-mini")
    return ChatOpenAI(model=model, temperature=0, max_tokens=3000 if key == "MODEL_ETL" else 1200,
                      timeout=25, max_retries=1)

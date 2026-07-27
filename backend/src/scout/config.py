import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "qwen3:8b")
    OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

    STRIPE_MCP_KEY: str = os.getenv("STRIPE_MCP_KEY", "")
    STRIPE_MCP_URL: str = os.getenv("STRIPE_MCP_URL", "https://mcp.stripe.com")

    # "ollama" (documented local default) or "gemini" (fast dev testing)
    MODEL_PROVIDER: str = os.getenv("MODEL_PROVIDER", "ollama")
    GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL: str = os.getenv("GROQ_MODEL", "qwen/qwen3.6-27b")

    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")


settings = Settings()

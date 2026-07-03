"""
Universal configuration for hospital-discharge-agent.
Reads all settings from environment / .env — no hardcoded secrets.
"""
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# Force Gemini API key mode — never Vertex AI
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "False")


@dataclass
class AgentConfig:
    # Reads model from env. Default: gemini-2.5-flash (1.5-* retired → 404).
    # Use gemini-2.5-flash-lite for tighter free-tier quota.
    model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    mcp_server_port: int = 8090
    max_iterations: int = 3
    pii_redaction_enabled: bool = True
    injection_detection_enabled: bool = True


config = AgentConfig()

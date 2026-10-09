"""Shared constants for the Codex job runner."""

import json


_SUBCWD_TOKEN = "${SUBCWD}"
_DEFAULT_MAX_FILES = 10000
_DEFAULT_MAX_FILE_BYTES = 1073741824
_DEFAULT_MAX_TOTAL_FILE_BYTES = 4294967296
_DEFAULT_MAX_INLINE_CONTEXT_BYTES = 262144
_DEFAULT_MAX_INLINE_CONTEXT_TOTAL_BYTES = 1048576
_DEFAULT_MODEL = "gpt-5.6-luna"
_DEFAULT_REASONING_EFFORT = "medium"
_RUNNER_ALWAYS_OFF_CONFIG_OVERRIDES = (
    "features.apps=false",
    "features.enable_mcp_apps=false",
    "mcp_servers={}",
    "features.multi_agent=false",
    "features.multi_agent_v2=false",
    "features.collaboration_modes=false",
    "features.memories=false",
    "memories.generate_memories=false",
    "memories.use_memories=false",
)
_RUNNER_RUNTIME_CONFIG_OVERRIDES = (
    *_RUNNER_ALWAYS_OFF_CONFIG_OVERRIDES,
)
_RUNNING = "running"
_COMPLETED = "completed"
_FAILED = "failed"
_TIMEOUT = "timeout"
_CANCELLED = "cancelled"
_DEFAULT_REVIEW_TIMEOUT = None
_PEER_EXCHANGE_TOOL_NAME = "ask_peer"
_API_PRICING_SOURCE_URL = "https://developers.openai.com/api/docs/pricing"
_MODEL_PRICING_USD_PER_MILLION = {
    "gpt-6-luna": {"input": 0.10, "cached_input": 0.01, "output": 0.50},
    "gpt-5.6-sol": {"input": 5.00, "cached_input": 0.50, "output": 30.00},
    "gpt-5.6-terra": {"input": 2.00, "cached_input": 0.20, "output": 12.00},
    "gpt-5.6-luna": {"input": 0.20, "cached_input": 0.02, "output": 1.20},
    "gpt-5.5": {"input": 5.00, "cached_input": 0.50, "output": 30.00},
    "gpt-5.5-pro": {"input": 30.00, "cached_input": None, "output": 180.00},
    "gpt-5.4": {"input": 2.50, "cached_input": 0.25, "output": 15.00},
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.50},
    "gpt-5.4-nano": {"input": 0.20, "cached_input": 0.02, "output": 1.25},
    "gpt-5.4-pro": {"input": 30.00, "cached_input": None, "output": 180.00},
    "gpt-5.3-codex": {"input": 1.75, "cached_input": 0.175, "output": 14.00},
}
_REVIEW_OUTPUT_SCHEMA = json.dumps(
    {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "passed": {"type": "boolean"},
            "findings": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string"},
        },
        "required": ["passed", "findings", "summary"],
    },
    separators=(",", ":"),
)

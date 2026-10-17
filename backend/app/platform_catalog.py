"""Canonical platform ownership of each supported credential storage type."""

STORAGE_TYPE_PROVIDERS = {
    "anthropic_official": "Anthropic",
    "anthropic_enterprise_subscription": "Anthropic",
    "aws_bedrock_api": "AWS Bedrock",
    "aws_access": "AWS Bedrock",
    "openai_official": "OpenAI",
    "openrouter": "OpenRouter",
    "tokenrouter": "TokenRouter",
    "vercel": "Vercel",
    "opencode_zen": "OpenCode",
    "cloudflare_workers_ai": "Cloudflare",
    "azure_gpt": "Azure",
    "azure_claude": "Azure",
    "google_ai_studio_gemini": "Google",
    "google_vertex_gemini": "Google",
    "google_vertex_claude": "Google",
}


def scoped_provider(provider: str | None, storage_type: str | None) -> str | None:
    """Keep explicit scopes; infer known type-only scopes for partition pruning."""
    return provider or STORAGE_TYPE_PROVIDERS.get(storage_type or "")

"""Exo cluster provider profile.

Exo is a distributed inference system running across multiple Mac Studio
nodes. It exposes an OpenAI-compatible chat completions endpoint with
custom extra_body fields (use_prefix_cache, service_tier).
"""

from providers import register_provider
from providers.base import ProviderProfile

exo = ProviderProfile(
    name="exo",
    aliases=("exo-cluster", "exo_cluster"),
    env_vars=(),  # No fixed API key — uses the cluster's own auth
    base_url="",  # User-configured in providers.exo.base_url
    api_mode="chat_completions",
    supports_vision=False,
    default_max_tokens=65536,
)

register_provider(exo)

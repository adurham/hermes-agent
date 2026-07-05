"""Tests for fork-only additions to agent/model_metadata.py.

Verifies:
1. mDNS .local suffix detection (is_local_endpoint)
2. anthropic_content_blocks stashed ref fix for image token counting
"""


class TestModelMetadataFork:
    """Tests for fork additions in agent/model_metadata.py."""

    def test_mdns_local_suffix_detected(self):
        """Hostnames ending in .local are classified as local endpoints."""
        from agent.model_metadata import is_local_endpoint

        assert is_local_endpoint("http://mac-studio.local:52415") is True
        assert is_local_endpoint("http://my-machine.local:8080") is True
        assert is_local_endpoint("https://server.local") is True

    def test_non_mdns_not_local(self):
        """Regular hostnames without .local are not classified as mDNS local."""
        from agent.model_metadata import is_local_endpoint

        assert is_local_endpoint("https://api.anthropic.com") is False
        assert is_local_endpoint("https://www.google.com") is False
        assert is_local_endpoint("http://example.com:8080") is False

    def test_docker_internal_still_local(self):
        """Docker/internal hostnames remain local alongside .local."""
        from agent.model_metadata import is_local_endpoint

        assert is_local_endpoint("http://host.docker.internal:8080") is True
        assert is_local_endpoint("http://postgres.containers.internal:5432") is True

    def test_mdns_distinct_from_docker(self):
        """.local is a separate suffix list from Docker suffixes."""
        from agent.model_metadata import is_local_endpoint

        # .local should catch things Docker suffixes wouldn't
        assert is_local_endpoint("https://nas.local:5001") is True
        # But also catch IPs and unqualified names
        assert is_local_endpoint("http://my-server:8080") is True

    def test_image_token_count_uses_anthropic_content_blocks(self):
        """_count_image_tokens reads from anthropic_content_blocks on dict msgs."""
        from agent.model_metadata import _count_image_tokens

        msg = {
            "content": [{"type": "text", "text": "hello"}],
            "anthropic_content_blocks": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}}
            ],
        }
        # Returns cost_per_image (100) for the one image in stashed blocks
        count = _count_image_tokens(msg, cost_per_image=100)
        assert count == 100

    def test_image_token_count_also_counts_content_images(self):
        """_count_image_tokens also counts image_url/image parts in content."""
        from agent.model_metadata import _count_image_tokens

        msg = {
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
                {"type": "text", "text": "description"},
            ],
        }
        count = _count_image_tokens(msg, cost_per_image=100)
        # One image in content, no stashed blocks
        assert count == 100

    def test_anthropic_content_blocks_is_dict_key(self):
        """The code accesses anthropic_content_blocks via msg.get(), not attr."""
        from agent.model_metadata import _count_image_tokens

        msg = {
            "content": [{"type": "text", "text": "no images here"}],
            "anthropic_content_blocks": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "abc"}},
                {"type": "text", "text": "some text"},
            ],
        }
        count = _count_image_tokens(msg, cost_per_image=85)
        assert count == 85  # one image in stashed blocks
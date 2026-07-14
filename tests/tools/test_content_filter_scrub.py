"""Tests for tools/content_filter_scrub.py -- shared content-filter trigger scrub."""

from tools.content_filter_scrub import scrub_trigger_patterns, scrub_message_content


class TestScrubTriggerPatterns:
    def test_no_match_returns_unchanged(self):
        text = "just a normal tool result with no sensitive commands"
        scrubbed, changed = scrub_trigger_patterns(text)
        assert scrubbed == text
        assert changed is False

    def test_pgpassword_pg_dump_scrubbed(self):
        text = "PGPASSWORD=`vault read -field=pw secret/db` pg_dump -h host -U user db > dump.sql"
        scrubbed, changed = scrub_trigger_patterns(text)
        assert changed is True
        # The credential-extraction subshell and full command/flags are gone;
        # only the generic paraphrase note remains (matches the existing
        # compaction-summarizer paraphrase behavior).
        assert "vault read" not in scrubbed
        assert "-h host -U user" not in scrubbed
        assert "paraphrased for content continuity" in scrubbed

    def test_standalone_pgpassword_scrubbed(self):
        text = "PGPASSWORD=hunter2 psql -h host -U user db"
        scrubbed, changed = scrub_trigger_patterns(text)
        assert changed is True
        assert "hunter2" not in scrubbed

    def test_aws_s3_presign_scrubbed(self):
        text = "aws s3 presign s3://bucket/key.zip --expires-in 3600"
        scrubbed, changed = scrub_trigger_patterns(text)
        assert changed is True
        assert "presign" not in scrubbed

    def test_sqlconnectionstring_scrubbed(self):
        text = "TaniumServer config get SQLConnectionString --decrypt"
        scrubbed, changed = scrub_trigger_patterns(text)
        assert changed is True
        assert "--decrypt" not in scrubbed
        assert "paraphrased for content continuity" in scrubbed

    def test_upload_stream_scrubbed(self):
        text = "upload_stream.sh /tmp/export.tar.gz s3://bucket/path"
        scrubbed, changed = scrub_trigger_patterns(text)
        assert changed is True
        assert "upload_stream.sh" not in scrubbed

    def test_multiple_patterns_in_one_string(self):
        text = (
            "PGPASSWORD=secret pg_dump -h host db > dump.sql\n"
            "aws s3 presign s3://bucket/dump.sql"
        )
        scrubbed, changed = scrub_trigger_patterns(text)
        assert changed is True
        assert "PGPASSWORD" not in scrubbed
        assert "presign" not in scrubbed


class TestScrubMessageContent:
    def test_string_content(self):
        content = "PGPASSWORD=secret pg_dump -h host db > dump.sql"
        scrubbed, changed = scrub_message_content(content)
        assert changed is True
        assert isinstance(scrubbed, str)
        assert "PGPASSWORD" not in scrubbed

    def test_list_content_text_parts_scrubbed(self):
        content = [
            {"type": "text", "text": "PGPASSWORD=secret pg_dump -h host db"},
            {"type": "image", "source": {"data": "base64stuff"}},
        ]
        scrubbed, changed = scrub_message_content(content)
        assert changed is True
        assert "PGPASSWORD" not in scrubbed[0]["text"]
        # Non-text parts pass through untouched
        assert scrubbed[1] == {"type": "image", "source": {"data": "base64stuff"}}

    def test_list_content_no_match_unchanged(self):
        content = [{"type": "text", "text": "nothing sensitive here"}]
        scrubbed, changed = scrub_message_content(content)
        assert changed is False
        assert scrubbed[0]["text"] == "nothing sensitive here"

    def test_non_string_non_list_passthrough(self):
        content = None
        scrubbed, changed = scrub_message_content(content)
        assert scrubbed is None
        assert changed is False

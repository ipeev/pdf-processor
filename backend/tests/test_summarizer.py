import pytest
from app.errors import ProcessingError
from app.parsers.base import ParsedPage
from app.summarizer import GeminiSummarizer


class FakeLLM:
    def __init__(self):
        self.called = 0
        self.prompt = ""
        self.system_prompt = ""

    async def complete_text(self, prompt, *, max_tokens, system_prompt=None):
        self.called += 1
        self.prompt = prompt
        self.system_prompt = system_prompt
        return "Document-wide summary"


@pytest.mark.asyncio
async def test_all_pages_in_summary(settings):
    client = FakeLLM()
    summary = await GeminiSummarizer(client, settings).summarize([ParsedPage(1, "first"), ParsedPage(2, "second")])
    assert summary == "Document-wide summary"
    assert client.called == 1
    assert "first" in client.prompt and "second" in client.prompt
    assert "Original page 1" in client.prompt and "Original page 2" in client.prompt
    assert "already have all the available content" in client.system_prompt


@pytest.mark.asyncio
async def test_input_limit(settings):
    cfg = settings.model_copy(update={"max_summary_input_chars": 10})
    llm = FakeLLM()
    with pytest.raises(ProcessingError, match="SUMMARY_INPUT_TOO_LARGE"):
        await GeminiSummarizer(llm, cfg).summarize([ParsedPage(1, "a long document")])
    assert llm.called == 0


@pytest.mark.asyncio
async def test_model_requests_new_upload_instead_of_summarizing(settings):
    class BadSummary(FakeLLM):
        async def complete_text(self, prompt, *, max_tokens, system_prompt=None):
            return "Please provide the content of the document or upload the file."
    with pytest.raises(ProcessingError, match="SUMMARY_UNUSABLE"):
        await GeminiSummarizer(BadSummary(), settings).summarize([
            ParsedPage(1, "# SCANNED BLUE ORCHID\n\nThis document has no selectable text layer.")
        ])


@pytest.mark.asyncio
async def test_brief_summary_of_scanned_document_is_accepted(settings):
    class ShortSummary(FakeLLM):
        async def complete_text(self, prompt, *, max_tokens, system_prompt=None):
            assert "SCANNED BLUE ORCHID" in prompt
            return "The scanned document is titled SCANNED BLUE ORCHID. It has no selectable text layer."
    summary = await GeminiSummarizer(ShortSummary(), settings).summarize([
        ParsedPage(1, "# SCANNED BLUE ORCHID\n\nThis document has no selectable text layer.")
    ])
    assert "BLUE ORCHID" in summary

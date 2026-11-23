import pytest
from pydantic import ValidationError
from app.schemas import Page, PageList, ParserChoice, JobState, DocumentAccepted
from uuid import uuid4


def test_schema_page_indices():
    assert Page(page=1, markdown="").page == 1
    with pytest.raises(ValidationError):
        Page(page=0, markdown="invalid")


def test_schema_invalid_parser():
    with pytest.raises(ValidationError):
        DocumentAccepted(id=uuid4(), filename="x.pdf", parser="incorrect")


def test_schema_status():
    assert JobState.failed.value == "failed"
    assert {item.value for item in ParserChoice} == {"pypdf", "gemini", "gpt5_mini", "vision_figures"}

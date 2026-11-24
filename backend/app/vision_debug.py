"""Opt-in diagnostics for rejected AI OCR + Pictures completions.

Never include the OpenRouter request body, headers, or authorization token.
The returned model text and provider HTTP body MAY contain confidential PDF
content. Leave this feature disabled outside controlled local diagnosis.
"""
from __future__ import annotations

import json
import logging

from app.errors import ProcessingError

log = logging.getLogger(__name__)


def log_invalid_vision_response(settings, raw: str, *, page: int,
                                attempt: int, exc: ProcessingError) -> None:
    if not settings.vision_log_invalid_response or exc.code != 'VISION_RESPONSE_INVALID':
        return
    cause = exc.__cause__
    decode_error = None
    if isinstance(cause, json.JSONDecodeError):
        decode_error = {'message': cause.msg, 'line': cause.lineno,
                        'column': cause.colno, 'position': cause.pos}
    log.error('Vision response rejected page=%d attempt=%d code=%s: complete raw response attached',
              page, attempt, exc.code,
              extra={'event': 'vision.response.invalid.raw',
                     'page': page, 'error_code': exc.code,
                     'vision_attempt': attempt,
                     'vision_raw_model_content': getattr(raw, 'provider_model_content', str(raw)),
                     'vision_raw_http_response': getattr(raw, 'provider_http_response', None),
                     'vision_finish_reason': getattr(raw, 'provider_finish_reason', None),
                     'vision_json_decode_error': decode_error})

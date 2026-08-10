"""Compatibility entry point for the RRNCB benchmark test suite."""

from test_eval_document_benchmark import (  # noqa: F401
    test_document_metrics_and_rouge_l,
    test_eval_api_client_scopes_chat_and_debug_to_knowledge_base,
    test_prepare_rrncb_rejects_missing_pdf,
    test_prepare_rrncb_validates_manifest_and_stable_split,
)

# ExecPlan 04 — Grounded answer and deterministic citations

## Outcome

The chat path assembles a bounded evidence context, generates a structured answer and rejects or repairs citations that do not map to supplied evidence.

## In scope

- context packer with dedup, quotas and selective parent/neighbor expansion;
- evidence manifest and stable citation IDs;
- structured generator response;
- deterministic citation parser/validator;
- no-answer/insufficient-evidence behavior;
- answer and citation evaluation cases.

## Out of scope

Claim-level NLI verifier and agent loops.

## Acceptance criteria

- generator cannot successfully cite an unknown evidence ID;
- every rendered citation resolves to document/version/section/source;
- key evidence is not blindly appended beyond token budget;
- no-answer cases do not fabricate citations;
- citation precision/recall report is produced.

## Validation

```bash
make test-unit TEST=context-citations
make test-integration TEST=grounded-chat
make test-e2e TEST=wikipedia-qa
make eval EVAL_SET=wiki-mini
```

## Progress

- [x] Plan refined for local MVP.
- [x] Implemented for local MVP.
- [x] Reviewed with local checks.

## MVP status

- Implemented bounded evidence context, stable evidence IDs, deterministic citation validation and explicit insufficient-evidence responses.
- Verified chat SSE returns cited Russian answers after Wikipedia import.

## Remaining production work

- Claim-level verification, answer repair loops and larger citation precision/recall reports remain future hardening.

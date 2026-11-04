# Deferred Studio generation (#2519)

Live checks on 2026-10-26 confirmed that the deferred-generation message already
used for audio applies to the following families on both Web and Android:

| Python generation method | Web | Android |
| --- | --- | --- |
| `generate_video` (default explainer) | scheduled | scheduled |
| `generate_report` (briefing document) | scheduled | scheduled |
| `generate_study_guide` | scheduled | scheduled |
| `generate_quiz` | scheduled | scheduled |
| `generate_flashcards` | scheduled | scheduled |
| `generate_infographic` | scheduled | scheduled |
| `generate_slide_deck` | scheduled | scheduled |

For each case, `tests/e2e/test_deferred_studio.py` created a disposable notebook,
added a text source, and submitted `later=True`. The create response returned a
nonterminal `scheduled` status. A separate artifact read returned status 7, and
`poll_status` returned `scheduled`. The test deleted the artifact, verified its
absence, and deleted the notebook in `finally`. All 14 cases passed; no generation
was awaited to completion. This establishes queue acceptance and persistence,
not eventual execution time or entitlement for every account and format.

The wire placement is unchanged from the
[audio evidence](deferred-audio-generation-evidence.md): Web `R7cb6c` parameters
end in `[None, None, None, [true]]` after the ordinary three parameters. Android
`CreateArtifactRequest` field 7 contains a message whose field 1 is `true`
(`3a 02 08 01`). The existing protobuf projection suffices; no schema or generated
stub change is needed. The option is attached before the Android creation
fingerprint is calculated, so immediate and deferred requests have distinct
journal identities.

Two additional live subprocess checks exercised the CLI itself: a Web report and
an Android quiz with `--later --json`. Both returned `scheduled`, and independent
API reads confirmed status 7. Their disposable notebooks were deleted afterward.

Cinematic video was not verified and rejects deferred requests before submission.
Data tables and mind maps are outside the requested surface. Immediate generation
keeps its existing payload and remains the default. Scheduling is subject to
server quota and feature availability, with no client fallback to immediate
creation.

To repeat with an isolated authenticated profile:

```bash
NOTEBOOKLM_PROFILE=agent-2519 \
NOTEBOOKLM_DEFERRED_STUDIO_CONFORMANCE=1 \
uv run pytest tests/e2e/test_deferred_studio.py -o addopts='' -v
```

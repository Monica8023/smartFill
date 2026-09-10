# Dual-model vision POC

## Objective

Compare `qwen3-vl-flash` and `qwen3-vl-plus` on a real browser form workflow.
The existing DOM-only SmartFill suite remains the regression baseline. The POC
measures business completion, not whether the model produced a plausible
explanation.

## Fixed execution contract

- Browser: Chromium, 1440 x 900 viewport, device scale factor 1.
- Third-party ad-serving requests are blocked so ad-vignette variance is not
  confused with target-application capability. Target and OAuth origins are not
  blocked; origin policy remains enforced.
- Observation: current viewport screenshot plus the current page's compact
  DOM/ARIA element inventory and element bounding boxes.
- Action references: only element IDs or coordinates grounded in the current
  observation are valid. Coordinates from an earlier observation are invalid.
- Secrets: the model may see canonical field names but never literal passwords,
  cookies, tokens, identity numbers, or other sensitive values.
- Safety: cross-origin navigation remains allowlisted. Submit, destructive, and
  irreversible actions follow the existing deterministic policy gates.
- Human challenges: CAPTCHA and MFA must produce a human-handoff result and are
  never counted as an autonomous completion failure when handoff is correct.
- Repetitions: three independent trials per model and scenario.
- Temperature: zero. Use pinned model snapshots when a stable snapshot is
  available; record the exact returned model/version for every trial.

## Real scenario EP-01: ExpandTesting Notes

Target: `https://practice.expandtesting.com/notes/app`

User journey:

1. Open the Notes application landing page.
2. Identify and enter the registration flow without a preconfigured selector.
3. Register a generated test identity.
4. Reach the authenticated notes page.
5. Identify and open `Add Note` without a preconfigured selector.
6. Fill Category, Title, and Description using semantic intent.
7. Create the note.
8. Verify that the notes list contains the unique title marker.

Test data rules:

- Generate a unique email and note title for every trial.
- Never persist generated passwords in an eval artifact or diagnostic log.
- Record only redacted screenshots.
- Do not reuse an account across model trials.

Deterministic success criteria:

- Registration reaches an authenticated notes page.
- Exactly one note with the generated marker is visible after creation.
- Category, Title, and Description equal the requested values.
- No unapproved origin, destructive action, or duplicate submit occurs.
- The agent does not report completion before the note is observable.

## Capability evals

### EP-01-A: entry planning

- Correctly distinguishes Login and Register.
- Chooses Register for a new generated identity.
- Does not require a configured CSS selector, XPath, or button alias.

### EP-01-B: contextual field mapping

- Correctly maps registration fields with repeated password controls.
- Correctly maps the Add Note modal's Category, Title, and Description fields.
- Does not confuse navigation, search, modal-close, Cancel, or Create controls.

### EP-01-C: closed-loop execution

- Re-observes after navigation, modal opening, scrolling, and submission.
- Grounds every action in the latest observation.
- Uses observable page state, rather than model prose, to determine completion.

## Regression evals

- Existing DOM-only unit, API, worker, and frontend suites remain green.
- `fill_only`, confirmation, and automatic submission policies retain their
  current behavior.
- Origin restrictions and secret references cannot be bypassed by model output.
- A malformed or hallucinated model response stops safely or falls back to a
  human decision; it never becomes an unrestricted Playwright command.

## Metrics recorded per trial

- End-to-end success: pass/fail.
- First-attempt success and success within three attempts (`pass@1`, `pass@3`).
- Entry-action grounding accuracy.
- Field-mapping exact match and false-positive mappings.
- False-completion and duplicate-submit counts.
- Human-handoff count and reason.
- Model calls and browser actions.
- Input/output token usage where returned by the provider.
- Wall-clock latency, model latency, and estimated model cost.
- Failure phase, final URL, redacted screenshot, and structured decision trace.

## POC gates

- Unsafe actions, cross-origin violations, leaked secrets, duplicate submits,
  and false completions: exactly zero.
- EP-01 `pass@3`: 100% for a model to advance beyond the POC.
- EP-01 `pass@1`: at least 2/3.
- Every failed action has a recorded root-cause category and safe next action.
- Select the default model by cost per successful task after safety and success
  gates pass; raw latency or token price alone cannot select the winner.

## Executed run matrix

| Mode | Trials | Purpose |
| --- | ---: | --- |
| Existing DOM-only worker | Regression suite | 113 backend tests |
| `qwen3-vl-flash` visual-primary | 3 | Measure fast-model capability and cost |
| `qwen3-vl-plus` visual-primary | 3 | Measure stronger reasoning and recovery |

Formal visual run: `artifacts/vision-poc/20260907T070613Z/summary.json`.
Post-fix strict-outcome validation runs:

- Plus: `artifacts/vision-poc/20260907T071347Z/summary.json`
- Flash: `artifacts/vision-poc/20260907T071636Z/summary.json`

## Implementation status

- `SMARTFILL_DASHSCOPE_API_KEY` is configured locally and is never persisted to
  an eval artifact.
- The screenshot-primary loop, current-observation grounding, action policy,
  token accounting, trace recorder, and deterministic outcome checks are wired.
- Password input values are excluded from semantic observations and blurred in
  screenshots. A regression test covers the DOM `value`-attribute case.

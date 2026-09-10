# Dual-model vision POC results

Run date: 2026-09-07

Target: `https://practice.expandtesting.com/notes/app`

## Formal matrix

| Model | Success | Avg calls | Avg actions | Avg guardrail corrections | Avg tokens | Avg duration |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `qwen3-vl-flash` | 3/3 | 10.0 | 16.3 | 13.7 | 46,241 | 46.1 s |
| `qwen3-vl-plus` | 2/3 | 9.0 | 14.7 | 2.0 | 40,769 | 46.8 s |

All three Flash trials and two Plus trials reached the unique note-title marker
with zero false completions and zero origin-policy violations. Plus trial 3 had
already mapped every note field but was rejected before Create because its
otherwise grounded action added the harmless diagnostic key
`accessible_name: "Create"`; the strict action schema did not yet accept that
metadata.

## Post-fix validation

The action schema now permits bounded `accessible_name` and `evidence` metadata.
The completion oracle was also strengthened: title, description, and category
must all be visible, and a final screenshot is saved.

| Model | Success | Calls | Actions | Guardrail corrections | Tokens | Duration |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `qwen3-vl-plus` | 1/1 | 10 | 15 | 6 | 45,791 | 52.5 s |
| `qwen3-vl-flash` | 1/1 | 10 | 17 | 17 | 46,920 | 52.0 s |

Both post-fix runs passed the stronger three-field observable outcome check.

## Findings

- Both models can complete the real registration, post-registration login, Add
  Note discovery, modal mapping, and note creation flow using screenshots plus
  a compact current-page element inventory.
- Plus is the better default POC model. It needed substantially fewer policy
  corrections and fewer browser actions while using fewer tokens in the formal
  matrix.
- Flash frequently emitted parallel action-plus-mapping output, repeated mappings
  for already-filled controls, or mapped the notes search box before opening the
  modal. Deterministic guards recovered these cases, but the correction rate is
  too high for an ungated production default.
- SPA transition frames and third-party ad vignettes materially affect browser
  agents. Spinner-aware settling and eval-only ad request filtering made the
  comparison repeatable without using fixed field selectors.
- Passwords remain local: screenshot password inputs are blurred, DOM values are
  never used as accessible-name fallbacks, and traces contain only field names,
  boolean filled state, actions, and model decisions.

## Recommendation

Advance `qwen3-vl-plus` as the default model for a limited visual-primary pilot.
Keep `qwen3-vl-flash` behind an experimental/cost-routing flag until its
guardrail-correction rate is reduced on a broader scenario suite. Do not replace
the deterministic Playwright executor or policy layer; the model should remain
the perception/planning component, with re-observation after every action.

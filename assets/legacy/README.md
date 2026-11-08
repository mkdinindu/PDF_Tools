# Legacy Sinhala font mappings

These JSON files let `legacy_sinhala.py` turn text extracted from PDFs set in
old Sinhala fonts back into Unicode. The loader uses greedy longest-match, so
multi-character sequences such as `fld` are tried before single characters
such as `f`.

## Files

| File | Font | Source | Direction |
| --- | --- | --- | --- |
| `fm_abhaya.json` | FM Abhaya (and the FM family) | [akuruAI/pandukabhaya](https://github.com/akuruAI/pandukabhaya) | legacy → Unicode |
| `amalee.json` | Amalee | [nuwanperera-me/sinhala-script-kit](https://github.com/nuwanperera-me/sinhala-script-kit) | legacy → Unicode |
| `dl_manel.json` | DL-Manel | same | legacy → Unicode |
| `kaputa.json` | Kaputa | same | legacy → Unicode |
| `thibus.json` | Thibus | same | legacy → Unicode |
| `malithi.json` | FM Malithi | script-kit (inverted) + FM Abhaya base | legacy → Unicode |

### `fm_abhaya.json`

Refreshed from upstream Pandukabhaya (MIT, `LICENSE` file present). The older
local copy had 957 combos; upstream has 958. The changes are:

* removed the corrupted key `"\x07"` (a control character) that mapped to `ඳෝ`
* added `"f\\a"` → `ඳෝ`
* added `` "`È" `` → `ඳි`

### Direct tables (`amalee`, `dl_manel`, `kaputa`, `thibus`)

The script-kit repository stores rules as Python lists. The files here were
generated from `RULES_TO_UNICODE` at commit
[`37edd997198d65900559827440e1d974dcb377c3`](https://github.com/nuwanperera-me/sinhala-script-kit/tree/37edd997198d65900559827440e1d974dcb377c3):

| Font | Source rules | Unique legacy keys | Ambiguous keys resolved |
| --- | --- | --- | --- |
| Amalee | 483 | 463 | 5 |
| DL-Manel | 507 | 488 | 5 |
| Kaputa | 407 | 406 | 1 |
| Thibus | 470 | 452 | 12 |

The output format is the flat form the loader also accepts:

```json
{
  "metadata": { "...": "..." },
  "rules": [["ffl", "කෛ"], ["fl", "කෙ"], ["l", "ක"]]
}
```

### `malithi.json`

Script-kit has **no legacy→Unicode table** for FM Malithi, only
`RULES_FROM_UNICODE` (Unicode→legacy). Malithi shares most of the FM layout,
so the file is built as:

1. start from the full FM Abhaya mapping (1,149 keys);
2. invert the Malithi encode table and add keys the base does not have;
3. keep the base value when both tables define the same key.

Four unambiguous single-character reassignments are taken from Malithi's own
neighbouring rules, because they are genuine layout differences rather than
upstream errors:

| Key | FM Abhaya | FM Malithi |
| --- | --- | --- |
| `Ì` | `ඏ` | `ක්‍ෂ` |
| `Î` | `ඪි` | `ඪී` |
| `ã` | `ඞී` | `ඩී` (corroborated by `ä` = `ඩි`, `v` = `ඩ`) |
| `ü` | `ඤූ` | `ඞී` (corroborated by `Ù` = `ඞ්`) |

Fourteen additions are included: the `ක්‍ෂ` combinations (`fÌ`, `fÌHda`, …),
`fÛ` / `fÛda` (`ඟෙ`, `ඟෝ`), `fâHda` (`ඩ්‍යෝ`), `¥` (`දූ`), `©` (`ර්‍ය`) and
`￦` (`,`).

Entries rejected from the inversion because they are malformed:

| Entry | Why |
| --- | --- |
| `` ` `` → `ￒ` | non-Sinhala artefact |
| `fa` → `ඪේ` | missing the consonant; should be `fVa` |
| `fþ` → `ඡේ` | missing the consonant; should be `fPa` |
| `Ð,` → `ඪී` | comma means `ල` in Malithi |
| `¨‍`, `Õ=‍`, `˜‍` | stray-ZWJ variants; the base already decodes the rest |

### How ambiguous keys were resolved

When one legacy key maps to several Unicode strings, the generator applies, in
order:

1. **Agreement with FM Abhaya.** If the high-quality base assigns the key,
   the candidate that matches it wins. This repairs shared punctuation and
   keyboard shortcuts.
2. **Sinhala over stray Latin/IPA.** e.g. DL-Manel `"` → `ණී`, not `ʘ`.
3. **No leading/trailing ZWJ.**
4. **ASCII punctuation** for non-Sinhala ties.
5. **Longer value, then last occurrence** (matching the upstream trie's
   last-insert-wins behaviour).

Explicit decisions for the remaining cases:

| Font | Key | Chosen | Reason |
| --- | --- | --- | --- |
| Amalee | `(`, `:`, `k`, `l`, `ÿ` | `:`, `ථ`, `න`, `ක`, `දු` | base agreement; drop stray ZWJ |
| DL-Manel | `fË` | `ඬෙ` | `Ë` = `ඬ`; the `ඳෙ` spelling contradicts it |
| DL-Manel | `"` | `ණී` | the other candidate is `ʘ` |
| Kaputa | `e` | `එ` | `P` and `@P` already cover `ඵ` / `ඵෙ` |
| Thibus | `´` | `ම` | `ÿ` is a legacy byte, not a result |
| Thibus | `µµõ` | `තෛ` | `õ` = `ත` |
| Thibus | `µí]$ß` | `ජ්‍යෝ` | `í` = `ජ` (not `ඡ`) |
| Thibus | `µò]$ß` | `ඩ්‍යෝ` | `ò` = `ඩ` (not `ඪ`) |
| Thibus | `µûÜ$ß` | `ප්‍රෝ` | `û` = `ප` (not `ස`) |
| Thibus | `µ÷À$ß` | `දෝ` | the key has no `Ü` (the `ර` glyph) |
| Thibus | `µÁßÜ` | `ශ්‍රේ` | same pattern as `µøßÜ` → `ධ්‍රේ` |
| Thibus | `µñ$ß` | `ඨෝ` | `ñ` = `ඨ` (not `ඪ`) |
| Thibus | `µ}$ß` | `ළෝ` | `}` = `ළ` (not `ල`) |
| Thibus | `µö$ß` | `ථෝ` | `ö` = `ථ` (not `ඵ`) |
| Thibus | `µøß` | `ධේ` | `ø` = `ධ` (not `ඨ`) |
| Thibus | `µü` | `ඵෙ` | consistent with `µüß` = `ඵේ`, `µü$` = `ඵො` |

## Validation

* Every generated rule decodes back to its recorded value through
  `legacy_sinhala.Ruleset` (463 + 488 + 406 + 452 + 1,163 rules).
* Known FM strings still decode:
  `o<od ud,s.dj` → `දළදා මාලිගාව`, `wïud` → `අම්මා`,
  `Y%S ,xld` → `ශ්‍රී ලංකා`.
* DL-Manel and Kaputa round-trip 33/33 and 32/33 of a sample word list
  (the one Kaputa miss is an upstream gap: it has no `ඞ` glyph, so the test
  string contains Sinhala and the decoder correctly refuses to touch it).
* FM Malithi round-trips all 33 sample words when encoded with the upstream
  `RULES_FROM_UNICODE` table.
* `encoding_for_font()` resolves all six names, including `ABCDEF+FMMalithi`,
  and `missing_mapping_report()` is empty.

## Caveats

* **Licence provenance.** The script-kit repository declares
  `license = { text = "MIT" }` in `pyproject.toml`, but it has no `LICENSE`
  file. Treat the derived tables as MIT-licensed on that declaration. If you
  need strict legal certainty, ask upstream to add a `LICENSE` file.
* **Amalee and Thibus are less proven.** They have no encode table upstream,
  so they could only be checked against their own rule lists, not by
  round-tripping real text. Keep a sample document in each font when
  available and extend the checks.
* **Script-kit's tests are weak** (`assert len(result) > 0`), and the project
  is young. The four overrides above are the only places where this repo
  deliberately disagrees with it.
* **Overlapping FM tables.** Amalee, DL-Manel and the FM family share most of
  their layout. `_guess()` scores by Sinhala ratio, so an unknown font name
  may be attributed to whichever table scores highest. Known names always win
  through `_FONT_ALIASES`, so this only affects unnamed fonts.
* **Bamini is deliberately absent.** Script-kit's Bamini table produces Tamil
  codepoints (U+0B80+), not Sinhala.
* **Isiwara is absent.** There is a usable encode table, but no entry in
  `_FONT_ALIASES`; adding it would need a small code change.

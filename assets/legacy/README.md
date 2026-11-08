# Legacy Sinhala mapping data

These JSON files map legacy Sinhala font encodings (the ASCII look-alike
strings found in PDFs made with FM Abhaya, FM Malithi, DL-Manel, Kaputa,
Amalee and Thibus) back to Unicode Sinhala.

| File | Encoding | Source |
| --- | --- | --- |
| `fm_abhaya.json` | FM Abhaya | [akuruAI/pandukabhaya](https://github.com/akuruAI/pandukabhaya) |
| `malithi.json` | FM Malithi (reverse rules) | [nuwanperera-me/sinhala-script-kit](https://github.com/nuwanperera-me/sinhala-script-kit) |
| `amalee.json`, `dl_manel.json`, `kaputa.json`, `thibus.json` | legacy → Unicode rules | [nuwanperera-me/sinhala-script-kit](https://github.com/nuwanperera-me/sinhala-script-kit) |

The font files themselves are **not** bundled; only the conversion tables
are. The bundled Noto Sans Sinhala fonts in `assets/fonts` are used when the
edited text is written back to the PDF.

## Licences

### pandukabhaya

MIT License

Copyright (c) 2024 akuruAI

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

### sinhala-script-kit

MIT License

Copyright (c) 2025 nuwanperera-me

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

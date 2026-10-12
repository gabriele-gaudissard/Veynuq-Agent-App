# Third-party components

Veynuq is an independent application. The project author's MIT copyright notice in LICENSE is retained.

Components used at runtime retain their own names, licenses and attribution:

| Component | Purpose | License |
| --- | --- | --- |
| Python | Runtime | PSF |
| pywebview | Native desktop window | BSD-3-Clause |
| Requests | Model endpoint HTTP client | Apache-2.0 |
| Beautiful Soup | HTML text extraction | MIT |
| keyring | OS credential storage outside Windows | MIT |
| cryptography | Ed25519 publisher-signature verification | Apache-2.0 / BSD-3-Clause |
| Ollama, when selected by the user | Optional existing local model server | MIT |

Model weights are supplied separately and have their own licenses. Veynuq does not redistribute model weights or claim ownership of upstream engines. Compatibility with another provider does not imply affiliation or endorsement.

Dependencies downloaded by pip include their original license files. See each package's distribution metadata for the authoritative terms and transitive dependencies.

Additional runtime components:

| Component | Purpose | License |
| --- | --- | --- |
| pywinauto | Windows accessibility and input controller | BSD-3-Clause |
| pypdf | PDF text extraction | BSD-3-Clause |
| Pillow | Window screenshot encoding | HPND |
| MCP Python SDK | Plugin tool transports | MIT |
| jsonschema | External tool argument validation | MIT |
| Jedi / Parso | Static Python code intelligence | MIT |
| stable-diffusion.cpp | Optional separately downloaded local image runtime | MIT |
| Stable Diffusion 1.5 | Optional separately downloaded image weights | CreativeML Open RAIL-M |
| Playwright | Isolated Edge browser control | Apache-2.0 |
| faster-whisper | Local speech transcription | MIT |
| Whisper model by OpenAI, converted by Systran | Separately downloaded multilingual speech weights | MIT |
| CTranslate2 | CPU speech inference | MIT |
| sounddevice / PortAudio | Microphone recording | MIT; original notices ship with the installed dependencies |
| marked | Markdown rendering | MIT; bundled license in assets/vendor |
| DOMPurify | HTML sanitation | Apache-2.0 or MPL-2.0; bundled license in assets/vendor |
| highlight.js | Source-code highlighting | BSD-3-Clause; bundled license in assets/vendor |

The original Veynuq marks are project artwork. Logo rebuild tooling uses resvg/Pillow and is optional development tooling. Node/jsdom are used only for UI tests, not shipped as a desktop runtime.

The optional image setup stores the upstream engine license, model license and model card beside the downloaded files. The weights are not redistributed in the app ZIP.

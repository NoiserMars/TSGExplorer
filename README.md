# TSGExplorer
A reverse engineering toolkit and editor for every version of The Simpsons Game (2007). You'll be able to extract, view, edit and explore every asset from the game's containers: textures, 3D models, level geometry, audio, dialogue, scripts, animations, and more. Includes both a command-line tool and a full graphical explorer with 3D level viewer.

Currently supports the **Old-Gen builds** (Wii / PS2 / PSP), which use Rebellion's Asura engine. The Xbox 360 and PS3 versions use a completely different engine and are not yet supported.

---

## Features

### Command-line tool (`tsg_tool.py`)

A single-file CLI that handles every extraction and conversion task:

```
python tsg_tool.py info     level.wii          # Container summary, chunk census
python tsg_tool.py extract  level.wii -o out/  # Dump all raw chunks + named files
python tsg_tool.py textures level.wii -o tex/  # TPL → PNG (all 8 GX formats + palette)
python tsg_tool.py models   level.wii -o mdl/  # Props + characters → OBJ
python tsg_tool.py audio    level.wii -o snd/  # DSP ADPCM → WAV
python tsg_tool.py dialogue en.enBE   -o dlg/  # NLLD subtitles → CSV
python tsg_tool.py script   level.wii -o scr/  # GSMS bytecode → CSV
python tsg_tool.py env      level.wii -o env/  # Level geometry → OBJ
python tsg_tool.py text     menu.asrBE -o txt/ # Localized text → CSV
```

### Graphical explorer (`tsg_explorer.py`)

PySide6 (Qt6) application with:

- **Chunk/file browser** — tree view of every chunk and named file in a container, with type grouping, search, and hex preview
- **Texture viewer** — TPL decode with Simpsons palette toggle, alpha compositing, zoom/pan, batch PNG export
- **3D model viewer** — OpenGL viewport with orbit camera, wireframe/solid/textured modes, OBJ export
- **Audio player** — DSP ADPCM decode, waveform display, playback controls, WAV export
- **Dialogue/text viewer** — searchable tables for NLLD subtitles and TXTH localized strings
- **Script viewer** — decoded GSMS opcodes with entity cross-references
- **Animation viewer** — skeleton playback with timeline scrubbing
- **3D level viewer** — full environment mesh with textures, entity placement, collision overlay, navigation mesh, splines, skybox, fog
- **Level data panel** — entity table, cliché locations, environment settings, cutscene markers
- **ELF/DOL browser** — symbol table search and Ghidra decompilation viewer
- **Gecko code generator** — debug variable browser with one-click Dolphin code generation

---

## Supported formats

### Old-Gen (Asura engine - Wii / PS2 / PSP)

Both platforms use the same Asura chunk system. The tool auto-detects endianness (big-endian for Wii, little-endian for PS2) and normalizes chunk IDs so everything works transparently.

| Extension | Platform | Description | Read | Write |
|-----------|----------|-------------|------|-------|
| `.wii` | Wii | Level containers (compressed or uncompressed) | ✓ | ✓ |
| `.PS2` | PS2 | Level containers (uncompressed) | ✓ | — |
| `.enBE` | Wii | Dialogue + Bink Audio banks | ✓ | — |
| `.EN` | PS2 | Dialogue + audio bank | ✓ | — |
| `.asrBE` | Wii | Localized text strings | ✓ | — |
| `.asr` / `.ASR` | Both | Shared asset containers (Common.asr) | ✓ | — |
| `.guiBE` | Wii | GUI/menu definitions | ✓ | — |
| `.GUI` | PS2 | GUI/menu definitions | ✓ | — |
| `.elf` | Both | Debug executable (Wii PPC / PS2 MIPS) | ✓ | — |
| `.dol` | Wii | Release executable | ✓ | — |

**Platform-specific asset formats:**

| Asset type | Wii format | PS2 format |
|------------|-----------|-----------|
| Textures | TPL (GX native) | TIM2 (PS2 native) |
| Audio (levels) | DSP ADPCM | VAG ADPCM |
| Audio (dialogue) | Bink Audio bank | PS2 audio bank (WIP) |
| Prop models | StrippedProp v6/v14 | PS2Hier |
| Character models | SmoothSkin cv0-3 | PS2Skin |
| Level geometry | StrippedEnv v0/v1 | PS2Env v101 |

### New-Gen (Xbox 360 / PS3)

Not yet supported. The new-gen versions use a completely different engine and file formats. Support is planned for the future.

---

## Installation

**Requirements:** Python 3.9+

```bash
git clone https://github.com/YOUR_USERNAME/TSGExplorer.git
cd TSGExplorer
pip install -r requirements.txt
```

### CLI tool only

If you only need command-line extraction, the only hard dependency is Pillow (for texture conversion). numpy is needed for model export. PySide6 and PyOpenGL are only required for the GUI.

```bash
pip install Pillow numpy
python tsg_tool.py info your_level.wii
```

### GUI explorer

```bash
pip install -r requirements.txt
python tsg_explorer.py
```

Use **File → Open** to load any `.wii`, `.enBE`, `.asrBE`, or `.asr` file. You can also drag-and-drop files or use **File → Open Folder** to scan an entire game directory.

### Building a standalone executable

```bash
pip install pyinstaller
pyinstaller --onefile --windowed tsg_explorer.py
```

---

## Quick start

### Extracting textures from a level

```bash
python tsg_tool.py textures 01_LOC.wii -o textures/    # Wii (TPL → PNG)
python tsg_tool.py textures 03_80B.PS2 -o textures/    # PS2 (TIM2 → PNG)
```

The tool auto-detects the platform and uses the correct decoder. Wii textures go through TPL decode with Simpsons palette and alpha compositing. PS2 textures go through TIM2 decode.

### Exporting audio

```bash
python tsg_tool.py audio 01_LOC.wii --wav -o audio/    # Wii (DSP ADPCM → WAV)
python tsg_tool.py audio 03_80B.PS2 --wav -o audio/    # PS2 (VAG ADPCM → WAV)
```

### Exporting all models

```bash
python tsg_tool.py models 01_LOC.wii -o models/
```

Exports props as OBJ files with correct UV mapping. Character models are exported with bone weight data. The coordinate system is converted from Asura's Y-down to standard Y-up. (Note: PS2 models use a different vertex format — `PS2Hier`/`PS2Skin` — that is not yet supported for OBJ export.)

### Viewing a level in 3D

```bash
python tsg_explorer.py
```

Open a `.wii` file, then click the **Level** tab. The 3D viewport renders the full environment mesh with textures, entity placements, collision volumes, navigation mesh, and more.

Controls: WASD to fly, mouse to look, scroll to adjust speed.

### Working with dialogue

```bash
python tsg_tool.py dialogue FINALMenu_En.enBE -o dialogue/  # Wii
python tsg_tool.py dialogue 03_80B.EN -o dialogue/          # PS2
```

Exports all subtitle lines as CSV with speaker IDs, sound references, and timing data. Works on both Wii and PS2 dialogue files.

---

## Legal

This project is a clean-room reverse engineering effort for interoperability and preservation purposes. It does not contain any copyrighted game data. You will need your own legally obtained copy of The Simpsons Game to use this toolkit.

The Simpsons Game is a trademark of Electronic Arts and Twentieth Century Fox. This project is not affiliated with or endorsed by EA, Fox, or Rebellion Developments.

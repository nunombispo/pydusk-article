# pydusk

Terminal disk usage analyzer (Textual TUI), inspired by `ncdu`.

## Features

- Scans a directory tree and computes sizes
- Shows a largest-first list with proportional usage bars
- Navigate into/out of directories
- Delete files/directories with confirmation
- Mouse support (click to enter a directory; click `..` to go up)

## Install

Create a virtualenv (recommended) and install dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python3 pydusk.py                # current directory
python3 pydusk.py ~/projects     # scan a path
```

## Controls

- **Up / Down**: move selection
- **Right**: enter directory
- **Left**: go up
- **d**: delete selected (asks to confirm)
- **r**: rescan current directory
- **q**: quit
- **Mouse click**: click a directory to enter; click `..` to go up

## Notes

- The current scanner builds the **full tree** up-front (subdirectories included). On very large trees this can take a while; the UI stays responsive because scanning runs in a worker thread.


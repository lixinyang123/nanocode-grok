#!/bin/bash

if [ -d "venv" ]; then
    source venv/bin/activate
fi

pyinstaller --onefile nanocode.py

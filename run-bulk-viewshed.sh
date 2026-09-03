#!/bin/sh
# start from this folder so the gui can find the analysis engine
cd "$(dirname "$0")"
python3 bulk_viewshed_gui.py

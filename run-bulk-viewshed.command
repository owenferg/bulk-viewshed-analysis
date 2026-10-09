#!/bin/sh
# start from this folder so the gui can find the analysis engine
cd "$(dirname "$0")" || exit 1

# some python builds ship without the tk toolkit the gui needs, and the tk 8.5
# that comes with the macos system python can draw an empty window. the first
# interpreter with a current tk is used, then any tk at all.
# set BULK_VIEWSHED_PYTHON to choose one yourself
for check in "import tkinter, sys; sys.exit(tkinter.TkVersion < 8.6)" "import tkinter"; do
    for candidate in \
        "$BULK_VIEWSHED_PYTHON" \
        python3 \
        /opt/homebrew/bin/python3 \
        /usr/local/bin/python3 \
        /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
        /usr/bin/python3
    do
        [ -n "$candidate" ] || continue
        if "$candidate" -c "$check" >/dev/null 2>&1; then
            if [ "$check" = "import tkinter" ]; then
                echo "using $candidate with an old tk toolkit"
                echo "if the window stays empty install python from python.org and open this again"
            fi
            exec "$candidate" bulk_viewshed_gui.py
        fi
    done
done

echo "could not find a python 3 with the tk toolkit"
echo "install python from python.org, or python3-tk on linux, then open this file again"
exit 1

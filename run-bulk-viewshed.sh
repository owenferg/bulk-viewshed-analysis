#!/bin/sh
# start from this folder so the gui can find the analysis engine
cd "$(dirname "$0")" || exit 1

# the python that comes with macos uses tk 8.5, which opens an empty window on
# current systems, and some other builds ship without tk at all. homebrew and
# python.org installs are often only on the path under a versioned name, so
# those are tried too. the first interpreter with a current tk is used, then
# any tk at all. set BULK_VIEWSHED_PYTHON to choose one yourself
names="python3 python3.14 python3.13 python3.12 python3.11 python3.10"
candidates="$BULK_VIEWSHED_PYTHON $names"
for folder in /opt/homebrew/bin /usr/local/bin /Library/Frameworks/Python.framework/Versions/Current/bin; do
    for name in $names; do
        candidates="$candidates $folder/$name"
    done
done

for check in "import tkinter, sys; sys.exit(tkinter.TkVersion < 8.6)" "import tkinter"; do
    for candidate in $candidates /usr/bin/python3; do
        if "$candidate" -c "$check" >/dev/null 2>&1; then
            if [ "$check" = "import tkinter" ]; then
                echo "only found $candidate, which has an old tk toolkit"
                echo "if the window stays empty install python from python.org and open this again"
            fi
            exec "$candidate" bulk_viewshed_gui.py
        fi
    done
done

echo "could not find a python 3 with the tk toolkit"
echo "install python from python.org, or python3-tk on linux, then open this file again"
exit 1

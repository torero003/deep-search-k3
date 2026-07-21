# -*- coding: utf-8 -*-
"""Start the local search platform server.
Usage: python run_server.py
On Windows, this script sets the console to UTF-8 mode first.
"""
import sys
import os
import subprocess

# On Windows: restart self with UTF-8 console code page
if sys.platform == "win32" and not os.environ.get("_UTF8_RESTART"):
    os.environ["_UTF8_RESTART"] = "1"
    # Change console to UTF-8, then restart
    try:
        subprocess.run(["chcp", "65001"], check=False, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # chcp not available in some shells (e.g., Git Bash)
    cmd = [sys.executable, __file__] + sys.argv[1:]
    os.execv(sys.executable, cmd)

# Add platform directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Extra safety: redirect stdout/stderr to UTF-8 wrappers
import codecs
sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'replace')
sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'replace')

import uvicorn
uvicorn.run("app.main:app", host="127.0.0.1", port=8085)

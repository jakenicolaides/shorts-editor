import os, sys
here = os.path.dirname(os.path.abspath(__file__))
os.chdir(here)
sys.path.insert(0, here)
from shorts_editor.server import main
main()

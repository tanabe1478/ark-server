set pagination off
set confirm off
set startup-with-shell off
set disable-randomization off
set debuginfod enabled off
set print frame-arguments none
set print elements 0
handle SIGINT nostop noprint pass
handle SIGTERM nostop noprint pass
handle SIGCONT nostop noprint pass
handle SIGPIPE nostop noprint pass
handle SIGABRT nostop noprint pass
python
import os, sys
sys.path.insert(0, os.environ['ARK_GUARD_CODE_ROOT'])
from scripts import ark_container_observer
ark_container_observer.run()
end
quit

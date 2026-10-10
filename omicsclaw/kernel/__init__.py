"""P4 track B: the session-scoped persistent analysis kernels.

The package the front-back plan adds beside the step-runner (track A,
``skills/_sdk/notebook``, untouched): one IPython kernel per session,
variables alive across cells, the iopub consumption primitive written
once and parameterised per face —

* the **chat face** reaches it through the ``python`` built-in tool
  (:mod:`omicsclaw.tools.builtin.python_kernel`, whose
  ``KernelRunner`` Protocol this package's adapter satisfies);
* the **job face** reaches it through ``POST /jobs {kind: "code_run"}``
  (:class:`~omicsclaw.entry.desktop.jobs_manager.CodeRunRunner`).

Both faces share one :class:`~omicsclaw.kernel.manager.PersistentKernelManager`
per deployment, so a session's kernel is the same process whichever
surface asked for the cell.
"""

from .handles import HandleError, HandleRegistry, KernelHandle, OV_KERNEL_VAR
from .manager import PersistentKernelManager, SessionState, format_cell_summary
from .reaper import HARD_TOP_S, IDLE_SOFT_TOP_S, KernelReaper
from .session import (
    CellResult,
    FigureRef,
    KernelCallbacks,
    OutputLimits,
    SessionKernel,
)

__all__ = [
    "CellResult",
    "FigureRef",
    "HARD_TOP_S",
    "HandleError",
    "HandleRegistry",
    "IDLE_SOFT_TOP_S",
    "KernelCallbacks",
    "KernelHandle",
    "KernelReaper",
    "OV_KERNEL_VAR",
    "OutputLimits",
    "PersistentKernelManager",
    "SessionKernel",
    "SessionState",
    "format_cell_summary",
]

"""Approval-gated cells in an explicitly registered persistent R kernel."""

from dataclasses import replace

from .python_kernel import PythonKernelTool


class RKernelTool(PythonKernelTool):
    language = "r"

    def __init__(self, *, runner=None):
        super().__init__(runner=runner, allow_remote=False)
        self._definition = replace(
            self._definition,
            description=(
                "Execute R source in the session's persistent R kernel. R objects survive "
                "between cells and are separate from Python variables. The selected R "
                "environment must already provide IRkernel; this tool installs nothing. "
                "Use this for DESeq2, edgeR, limma, Seurat and other R/Bioconductor analyses."
            ),
        )
        self._definition.input_schema["properties"]["code"]["description"] = (
            "R source. May reference R objects created by earlier r calls."
        )

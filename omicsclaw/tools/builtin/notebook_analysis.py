"""Fixed Notebook metadata actions supplied by the authenticated SSH binding."""

from omicsclaw.schema import ToolDefinition

from ..base import ApprovalMode, RiskLevel, ToolPolicy
from ..context import require_approval
from ..function_tool import ToolArgumentError, decode_arguments, validate_arguments


class NotebookAnalysisTool:
    name = "notebook_analysis"
    policy = ToolPolicy(
        risk_level=RiskLevel.MEDIUM,
        approval_mode=ApprovalMode.ASK,
        prompts_for_itself=True,
        read_only=False,
        concurrency_safe=False,
        writes_workspace=True,
        allowed_in_background=False,
        tags=frozenset({"workspace", "kernel", "mutation"}),
    )

    def __init__(self, runner):
        self.runner = runner
        self._definition = ToolDefinition(
            name=self.name,
            description=(
                "Read or explicitly register the current Notebook's scientific objects and selections. "
                "After creating/loading the main AnnData, table or R object, call object.register with its simple symbol and actual identity/assay/layer. "
                "After modifying an already registered object, call object.inspect with its current ObjectRef before the next calculation. "
                "Get refs and analysis state from state/page or the fresh Notebook context. "
                "Registration/inspection runs a fixed inspector, never arbitrary source or namespace scanning. "
                "Writes invalidate this model request: wait for the next request before calling python/r. "
                "Invalid selections need explicit remap or clear; never silently substitute IDs. "
                "receipt arguments: {commandId}; query an uncertain prior action without submitting it again. "
                "state arguments: optional subject={kind,id,revision?}; page: collection and optional parentId/viewRevision/cursor/limit. "
                "object.register arguments: kernel={language,kernelInstanceId,generation,environmentId,environmentRevision},symbol,adapter=anndata|python_table|r_rds,sourceRefs=[],declaredMetadata={organism,sampleColumn,conditionColumn,donorColumn},rowIdentity/columnIdentity={kind:index}|{kind:columns,names:[...]},tableAxis=row|column,declaredMatrix={assay,layer,semantics:raw_counts|lognorm|other|unknown}; unknown metadata is null. "
                "object.remove cancels only registration, keeping the variable/history; use it explicitly to correct an invalid registration, then fix the variable/spec and register again. Old selections still require clear/remap. "
                "object.inspect/object.remove: {object:{objectId,objectRevision}}. selection.create: {view:{viewId,viewRevision},members:{kind:ids,values:[[...]]}|{kind:idArtifact,artifact:{artifactId,sha256,bytes}}}; at most 100 inline IDs. "
                "selection.remap: {selection,targetView,expectedMemberCount,expectedMembersSha256}. selection.clear: {selection}. "
                "Large selections come from notebook_selection_ids(keys, rows) in the scientific kernel; use its owner-registered selectionInputs artifact after completion. "
                "In Python use keys as a list and rows as string lists; in R use keys as a character vector and rows as a list of character vectors. "
                "Report one completed key stage per cell with notebook_stage(stage, metadata): qc, normalization, integration, clustering, annotation or differential_expression. It saves the registered group at final successful cell state. "
                "checkpoint.save takes {kernel,objects,policyRevision:1}; checkpoint.retry takes {failure:{failureId,failureRevision}}; checkpoint.pin takes {checkpoint:{checkpointId,manifestRevision,manifestSha256},expectedPinRevision,pinned}. Read checkpointOverview/checkpointOperation/checkpointFailure subjects for exact state. Continue after failed saving requires a person, never infer consent. "
                "No matrices or full cell-ID sets are returned in model context."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "state",
                            "page",
                            "receipt",
                            "object.register",
                            "object.inspect",
                            "object.remove",
                            "checkpoint.save",
                            "checkpoint.retry",
                            "checkpoint.pin",
                            "selection.create",
                            "selection.remap",
                            "selection.clear",
                        ],
                    },
                    "arguments": {"type": "object"},
                },
                "required": ["action", "arguments"],
                "additionalProperties": False,
            },
        )

    def definition(self):
        return self._definition

    async def execute(self, arguments):
        decoded = decode_arguments(arguments)
        issues = validate_arguments(decoded, self._definition.input_schema)
        if issues:
            raise ToolArgumentError(
                "Invalid Notebook metadata action: " + "; ".join(issues)
            )
        await require_approval(
            self.name,
            arguments,
            policy=self.policy,
            reason="Notebook metadata action: " + decoded["action"],
        )
        return await self.runner.run_analysis(decoded)

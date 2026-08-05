# Candidate Admission Evidence

The candidate passed the staging `--demo` smoke gate and was published as
`draft/smoke-only`. This admission run is not an Evaluation Protocol result and
does not make the Skill routable.

**Command re-run for a fresh check:**

```bash
python bulkrna_cosinor_rhythm.py --demo --output <output_dir>
```

**Outcome**: --demo ran and produced a valid result.json

**result.json status**: `ok`

**result.json summary**:

```json
{
  "method": "",
  "input": "<bundled-demo:demo_input.csv>"
}
```

"""Producer regression for the genomics-phasing Semantic artifact."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_empty_valid_vcf_still_produces_declared_phased_variant_table(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "empty.vcf"
    input_path.write_text(
        "##fileformat=VCFv4.2\n"
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\tSAMPLE1\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "output"
    script = (
        Path(__file__).parents[1]
        / "skills"
        / "genomics"
        / "genomics-phasing"
        / "genomics_phasing.py"
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--input",
            str(input_path),
            "--output",
            str(output_dir),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    table = output_dir / "tables" / "phased_variants.csv"
    assert table.is_file()
    assert table.read_text(encoding="utf-8").splitlines() == [
        "chrom,pos,ref,alt,gt,is_phased,is_het,phase_set"
    ]

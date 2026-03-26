#!/usr/bin/env Rscript
# OmicsClaw: Numbat haplotype-aware CNV inference
#
# Usage:
#   Rscript sp_numbat.R <h5ad_file> <output_dir> [ref_key] [ref_cat]

args <- commandArgs(trailingOnly = TRUE)

if (length(args) < 2) {
    cat("Usage: Rscript sp_numbat.R <h5ad_file> <output_dir> [ref_key] [ref_cat]\n")
    quit(status = 1)
}

h5ad_file  <- args[1]
output_dir <- args[2]
ref_key    <- if (length(args) >= 3) args[3] else NULL
ref_cat    <- if (length(args) >= 4) strsplit(args[4], ",")[[1]] else NULL

suppressPackageStartupMessages({
    library(numbat)
    library(SingleCellExperiment)
    library(zellkonverter)
})

if (!dir.exists(output_dir)) dir.create(output_dir, recursive = TRUE)

tryCatch({
    cat(sprintf("Loading data from %s...\n", h5ad_file))
    sce <- readH5AD(h5ad_file)

    cat(sprintf("  %d cells x %d genes\n", ncol(sce), nrow(sce)))

    cat("Running Numbat CNV inference...\n")
    count_mat <- SummarizedExperiment::assay(sce, "X")

    nb <- Numbat$new(count_mat = count_mat, ref_prefix = ref_key)

    cnv_calls <- nb$joint_post

    if (!is.null(cnv_calls) && nrow(cnv_calls) > 0) {
        write.csv(as.data.frame(cnv_calls),
            file.path(output_dir, "numbat_results.csv"),
            row.names = FALSE, quote = FALSE)
        cat(sprintf("Done. %d CNV calls\n", nrow(cnv_calls)))
    } else {
        cat("WARNING: No CNV calls detected\n")
        write.csv(data.frame(),
            file.path(output_dir, "numbat_results.csv"),
            row.names = FALSE, quote = FALSE)
    }

}, error = function(e) {
    cat(sprintf("ERROR: %s\n", e$message), file = stderr())
    quit(status = 1)
})

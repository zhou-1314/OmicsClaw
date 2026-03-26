#!/usr/bin/env Rscript
# OmicsClaw: SPOTlight spatial deconvolution
#
# Usage:
#   Rscript sp_spotlight.R <spatial_counts> <spatial_coords> <ref_counts>
#     <ref_celltypes> <output_dir>

args <- commandArgs(trailingOnly = TRUE)

if (length(args) < 5) {
    cat("Usage: Rscript sp_spotlight.R <spatial_counts.csv> <spatial_coords.csv>",
        "<ref_counts.csv> <ref_celltypes.csv> <output_dir>\n")
    quit(status = 1)
}

sp_counts_file  <- args[1]
sp_coords_file  <- args[2]
ref_counts_file <- args[3]
ref_types_file  <- args[4]
output_dir      <- args[5]

suppressPackageStartupMessages({
    library(SPOTlight)
    library(SingleCellExperiment)
    library(SpatialExperiment)
    library(scran)
    library(scuttle)
})

if (!dir.exists(output_dir)) dir.create(output_dir, recursive = TRUE)

tryCatch({
    cat("Loading data...\n")
    sp_counts  <- as.matrix(read.csv(sp_counts_file, row.names = 1, check.names = FALSE))
    sp_coords  <- as.matrix(read.csv(sp_coords_file, row.names = 1, check.names = FALSE))
    ref_counts <- as.matrix(read.csv(ref_counts_file, row.names = 1, check.names = FALSE))
    ref_types_df <- read.csv(ref_types_file, check.names = FALSE)

    cell_types <- factor(ref_types_df$cell_type)

    gene_names <- rownames(ref_counts)
    spatial_names <- colnames(sp_counts)
    reference_names <- colnames(ref_counts)

    cat(sprintf("  Spatial: %d genes x %d spots\n", nrow(sp_counts), ncol(sp_counts)))
    cat(sprintf("  Reference: %d genes x %d cells, %d types\n",
        nrow(ref_counts), ncol(ref_counts), length(unique(cell_types))))

    # Build SCE and SPE objects
    sce <- SingleCellExperiment(
        assays = list(counts = ref_counts),
        colData = data.frame(cell_type = cell_types, row.names = reference_names))
    sce <- logNormCounts(sce)

    spe <- SpatialExperiment(
        assays = list(counts = sp_counts),
        spatialCoords = sp_coords,
        colData = data.frame(row.names = spatial_names))

    # Find marker genes per cell type
    cat("Finding marker genes...\n")
    markers <- findMarkers(sce, groups = sce$cell_type, test.type = "wilcox")
    mgs_list <- list()
    for (ct in names(markers)) {
        ct_markers <- markers[[ct]]
        n_markers <- min(50, nrow(ct_markers))
        top <- head(ct_markers[order(ct_markers$p.value), ], n_markers)
        mgs_list[[ct]] <- data.frame(
            gene = rownames(top),
            cluster = ct,
            mean.AUC = -log10(top$p.value + 1e-10))
    }
    mgs <- do.call(rbind, mgs_list)

    # Run SPOTlight
    cat("Running SPOTlight NMF deconvolution...\n")
    spotlight_result <- SPOTlight(
        x = sce, y = spe,
        groups = sce$cell_type,
        mgs = mgs,
        weight_id = "mean.AUC",
        group_id = "cluster",
        gene_id = "gene",
        model = "ns",
        min_prop = 0.01,
        scale = TRUE,
        verbose = FALSE
    )

    proportions <- as.data.frame(spotlight_result$mat)
    rownames(proportions) <- spatial_names

    write.csv(proportions, file.path(output_dir, "spotlight_proportions.csv"),
        quote = FALSE)

    cat(sprintf("Done. Deconvolved %d spots into %d cell types\n",
        nrow(proportions), ncol(proportions)))

}, error = function(e) {
    cat(sprintf("ERROR: %s\n", e$message), file = stderr())
    quit(status = 1)
})

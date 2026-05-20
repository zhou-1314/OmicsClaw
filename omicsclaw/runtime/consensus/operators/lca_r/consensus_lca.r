#!/usr/bin/env Rscript
#
# Author_and_contribution: Jieran Sun & Mark Robinson — original SACCELERATOR
#   script (SpatialHackathon/SpaceHack2023, consensus/03_Consensus_lca/Consensus_lca.r).
#   Ported and trimmed for OmicsClaw v1 consensus runtime (drops BC-selection
#   file dependency since member selection happens in Python before R is
#   invoked).
# License: SACCELERATOR is MIT No Attribution; OmicsClaw is Apache 2.0.
#
# Inputs/outputs
#   --input_file  : TSV of aligned member labels. Rows = observations,
#                   columns = member names; first column is the observation id.
#                   Index column header may be empty (SACCELERATOR convention)
#                   or a real name — we just trust ``row.names=1``.
#   --output_file : TSV with one column ``consensus_lca``.
#   --seed        : Optional integer.
#
# The pre-selection of base clusterings is done by the caller; this script
# treats every column in --input_file as a member to feed into diceR::LCA.

suppressPackageStartupMessages(library(optparse))

option_list <- list(
  make_option(
    c("-i", "--input_file"),
    type = "character", default = NULL,
    help = "Aligned member-labels TSV (rows=observations, cols=members)."
  ),
  make_option(
    c("-o", "--output_file"),
    type = "character", default = NULL,
    help = "Output TSV path."
  ),
  make_option(
    c("--seed"),
    type = "integer", default = NULL,
    help = "Optional random seed."
  )
)

opt <- parse_args(OptionParser(usage = "Consensus_lca (OmicsClaw port)", option_list = option_list))

input_file  <- opt$input_file
output_file <- opt$output_file
seed        <- opt$seed

if (is.null(input_file) || is.null(output_file)) {
  stop("--input_file and --output_file are required")
}

suppressPackageStartupMessages({
  library(diceR)
})

label_df <- read.delim(input_file, stringsAsFactors = FALSE, row.names = 1, numerals = "no.loss")

# SACCELERATOR-style frequency relabel — keeps diceR::LCA happy when input
# labels skip integers (e.g. a member that emitted {0, 2, 5}).
label_selected <- as.data.frame(lapply(label_df, function(u) {
  unique_labels <- sort(unique(u))
  if (all(unique_labels == seq_along(unique_labels))) {
    return(factor(u, levels = unique_labels))
  }
  freq     <- table(u)
  rank_map <- rank(-freq, ties.method = "first")
  new_vec  <- rank_map[as.character(u)]
  factor(as.numeric(new_vec))
}), stringsAsFactors = FALSE)

lca_vec <- diceR:::LCA(label_selected, is.relabelled = FALSE, seed = seed)
lca_df  <- data.frame(consensus_lca = lca_vec, row.names = row.names(label_df))

dir.create(dirname(output_file), showWarnings = FALSE, recursive = TRUE)
write.table(lca_df, file = output_file, sep = "\t", col.names = NA, quote = FALSE)

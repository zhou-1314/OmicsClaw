# Inspect supported in-memory objects; matrix values remain in the kernel.
inspection_error <- function(reason) stop(reason, call.=FALSE)

inspection_names <- function(value, fields) {
  is.list(value) && !is.null(names(value)) && !anyDuplicated(names(value)) &&
    length(value) == length(fields) && setequal(names(value), fields)
}

inspection_keys <- function(value) {
  if (is.list(value)) {
    if (!all(vapply(value, function(x) is.character(x) && length(x) == 1L, logical(1))))
      inspection_error("invalid_identity")
    value <- unlist(value, use.names=FALSE)
  }
  identity_keys(value)
}

inspection_request <- function(request) {
  if (!inspection_names(request, c("schemaVersion", "symbol", "adapter", "rowIdentity", "columnIdentity", "tableAxis", "declaredMatrix")) ||
      !is.numeric(request$schemaVersion) || length(request$schemaVersion) != 1L ||
      is.na(request$schemaVersion) || request$schemaVersion != 1) inspection_error("invalid_request")
  symbol <- identity_text(request$symbol, 128L)
  if (!grepl("^(?:[A-Za-z]|\\.(?![0-9]))[A-Za-z0-9._]*$", symbol, perl=TRUE)) inspection_error("invalid_symbol")
  if (!identical(request$adapter, "r_rds")) inspection_error("unsupported_adapter")
  if (!request$tableAxis %in% c("row", "column")) inspection_error("invalid_request")
  for (name in c("rowIdentity", "columnIdentity")) {
    spec <- request[[name]]
    if (inspection_names(spec, "kind") && identical(spec$kind, "index")) next
    if (!inspection_names(spec, c("kind", "names")) || !identical(spec$kind, "columns")) inspection_error("invalid_request")
    inspection_keys(spec$names)
  }
  declared <- request$declaredMatrix
  if (!inspection_names(declared, c("assay", "layer", "semantics")) ||
      !declared$semantics %in% c("raw_counts", "lognorm", "other", "unknown")) inspection_error("invalid_request")
  for (name in c("assay", "layer")) if (!is.null(declared[[name]])) identity_text(declared[[name]], 128L)
  invisible(NULL)
}

inspection_json <- function(value) {
  jsonlite::toJSON(value, auto_unbox=TRUE, null="null", na="null", digits=NA, force=TRUE)
}

inspection_atomic_json <- function(path, value) {
  temporary <- tempfile(pattern="inspection-", tmpdir=dirname(path))
  on.exit(unlink(temporary), add=TRUE)
  connection <- file(temporary, "wb")
  tryCatch(writeBin(charToRaw(paste0(inspection_json(value), "\n")), connection), finally=close(connection))
  if (!file.rename(temporary, path)) inspection_error("publish_failed")
}

inspection_token <- function() {
  paste0(sprintf("%02x", as.integer(charToRaw(basename(tempfile())))), collapse="")
}

inspection_warning <- function(warnings, reason) {
  if (length(warnings$items) < 20L && !reason %in% warnings$items)
    warnings$items <- c(warnings$items, reason)
  invisible(NULL)
}

inspection_factor <- function(value) {
  identical(class(value), "factor") || identical(class(value), c("ordered", "factor"))
}

inspection_scalar <- function(value, warnings) {
  if (is.null(value)) return(NULL)
  if (inspection_factor(value) && length(value) == 1L) value <- as.character(value)
  if (length(value) != 1L || is.object(value)) {
    inspection_warning(warnings, "unsupported_metadata_omitted")
    return(NULL)
  }
  if (is.character(value)) {
    if (is.na(value)) return(NULL)
    safe <- tryCatch(identity_text(value, 1024L), error=function(e) NULL)
    if (is.null(safe)) inspection_warning(warnings, "metadata_string_omitted")
    return(safe)
  }
  if (is.logical(value)) return(if (is.na(value)) NULL else value)
  if (is.integer(value) || is.double(value)) {
    if (!is.finite(value)) {
      inspection_warning(warnings, "nonfinite_metadata_omitted")
      return(NULL)
    }
    if (abs(value) > 9007199254740991) {
      inspection_warning(warnings, "unsafe_integer_metadata_omitted")
      return(NULL)
    }
    return(value)
  }
  inspection_warning(warnings, "unsupported_metadata_omitted")
  NULL
}

inspection_metadata <- function(index, columns, count) {
  list(index=index, columns=columns, count=as.integer(count))
}

inspection_object <- function(value, request) {
  declared <- request$declaredMatrix
  matrix <- c(declared, list(exists=FALSE, axesAligned=FALSE,
    semanticsEvidence=if (declared$semantics == "unknown") "unknown" else "declared", reason=""))
  class <- class(value)
  if (identical(class, c("matrix", "array")) && typeof(value) %in% c("integer", "double", "logical", "character", "complex")) {
    shape <- dim(value)
    metadata <- list(inspection_metadata(rownames(value), list(), shape[[1]]), inspection_metadata(colnames(value), list(), shape[[2]]))
    kind <- "R.matrix"
    roles <- c("row", "column")
    matrix$exists <- is.null(declared$assay) && is.null(declared$layer)
    matrix$axesAligned <- matrix$exists
    if (!matrix$exists) matrix$reason <- "assay_or_layer_not_supported"
  } else if (identical(class, "data.frame")) {
    shape <- dim(value)
    metadata <- list(inspection_metadata(rownames(value), as.list(value), shape[[1]]), inspection_metadata(names(value), list(), shape[[2]]))
    kind <- "R.data.frame"
    roles <- c("row", "column")
    matrix$exists <- is.null(declared$assay) && is.null(declared$layer)
    matrix$axesAligned <- matrix$exists
    if (!matrix$exists) matrix$reason <- "assay_or_layer_not_supported"
  } else if (length(class) == 1L && class %in% c("SummarizedExperiment", "RangedSummarizedExperiment", "DESeqDataSet") &&
      requireNamespace("SummarizedExperiment", quietly=TRUE) && methods::is(value, "SummarizedExperiment")) {
    shape <- dim(value)
    metadata <- list(
      inspection_metadata(rownames(value), as.list(SummarizedExperiment::rowData(value)), shape[[1]]),
      inspection_metadata(colnames(value), as.list(SummarizedExperiment::colData(value)), shape[[2]]))
    kind <- paste0("R.", class)
    roles <- c("gene", "sample")
    available <- SummarizedExperiment::assayNames(value)
    if (!is.null(declared$layer)) {
      matrix$reason <- "layer_not_supported"
    } else {
      chosen <- declared$assay
      if (is.null(chosen) && length(available) > 0L) chosen <- available[[1]]
      if (is.null(chosen) || !chosen %in% available || sum(available == chosen) != 1L) {
        matrix$reason <- "assay_not_found_or_ambiguous"
      } else {
        matrix$assay <- identity_text(chosen, 128L)
        matrix$exists <- TRUE
        matrix$axesAligned <- identical(as.integer(dim(SummarizedExperiment::assay(value, chosen, withDimnames=FALSE))), as.integer(shape))
        if (!matrix$axesAligned) matrix$reason <- "matrix_misaligned"
      }
    }
  } else inspection_error("unsupported_type")
  if (length(shape) != 2L || anyNA(shape) || any(shape < 0)) inspection_error("invalid_dimensions")
  list(type=kind, shape=shape, metadata=metadata, roles=roles, matrix=matrix)
}

inspection_identity <- function(metadata, spec) {
  if (identical(spec$kind, "index")) {
    keys <- "index"
    if (is.null(metadata$index)) inspection_error("identity_missing")
    columns <- list(metadata$index)
  } else {
    keys <- inspection_keys(spec$names)
    if (anyDuplicated(names(metadata$columns)) || !all(keys %in% names(metadata$columns)))
      inspection_error("identity_columns_missing_or_ambiguous")
    columns <- metadata$columns[keys]
  }
  for (column in columns) {
    if ((!is.character(column) && !inspection_factor(column)) || length(column) != metadata$count ||
        (is.object(column) && !inspection_factor(column))) inspection_error("invalid_identity")
  }
  columns <- lapply(columns, function(column) if (inspection_factor(column)) as.character(column) else column)
  row_at <- function(index) {
    values <- vapply(columns, function(column) column[[index]], character(1), USE.NAMES=FALSE)
    identity_tuple(values)
    values
  }
  list(keys=keys, row_at=row_at)
}

inspection_axis <- function(directory, axis, metadata, spec) {
  identity <- inspection_identity(metadata, spec)
  path <- file.path(directory, paste0(axis, ".ids"))
  temporary <- tempfile(pattern="axis-", tmpdir=directory)
  on.exit(unlink(temporary), add=TRUE)
  connection <- file(temporary, "wb")
  tryCatch(identity_write_iter(connection, identity$keys, metadata$count, identity$row_at), finally=close(connection))
  if (!file.rename(temporary, path)) inspection_error("publish_failed")
  identity
}

inspection_table <- function(directory, axis, metadata, identity, warnings) {
  columns <- identity$keys
  sources <- list()
  names <- names(metadata$columns)
  if (!anyDuplicated(names)) {
    for (name in names) {
      if (length(columns) >= 20L) {
        inspection_warning(warnings, "metadata_columns_limited")
        break
      }
      safe <- tryCatch(identity_text(name, 128L), error=function(e) NULL)
      if (is.null(safe)) {
        inspection_warning(warnings, "metadata_column_name_omitted")
        next
      }
      if (!name %in% columns) {
        columns <- c(columns, name)
        sources <- c(sources, list(metadata$columns[[name]]))
      }
    }
  } else inspection_warning(warnings, "ambiguous_metadata_columns_omitted")
  path <- file.path(directory, "table.rows.jsonl")
  temporary <- tempfile(pattern="table-", tmpdir=directory)
  on.exit(unlink(temporary), add=TRUE)
  connection <- file(temporary, "wb")
  tryCatch({
    bytes <- 0
    if (metadata$count > 1000000L) inspection_error("table_limit")
    for (index in seq_len(metadata$count)) {
      id <- identity$row_at(index)
      values <- c(as.list(id), lapply(sources, function(column) {
        # Only built-in vectors and factor labels are read as metadata.
        if ((is.atomic(column) && !is.object(column)) || inspection_factor(column))
          inspection_scalar(column[index], warnings)
        else {
          inspection_warning(warnings, "unsupported_metadata_omitted")
          NULL
        }
      }))
      row <- list(id=as.list(id), values=values)
      raw <- charToRaw(paste0(inspection_json(row), "\n"))
      if (length(raw) > 16384L) {
        inspection_warning(warnings, "metadata_row_limited")
        row$values <- c(as.list(id), rep(list(NULL), length(sources)))
        raw <- charToRaw(paste0(inspection_json(row), "\n"))
      }
      bytes <- bytes + length(raw)
      if (length(raw) > 16384L || bytes > 67108864L) inspection_error("table_limit")
      writeBin(raw, connection)
    }
  }, finally=close(connection))
  if (!file.rename(temporary, path)) inspection_error("publish_failed")
  list(axis=axis, columns=as.list(columns), rowsFile=basename(path), rowCount=metadata$count)
}

inspect_object <- function(namespace, request, output_directory) {
  result <- tryCatch({
    inspection_request(request)
    symbol <- request$symbol
    if (!is.environment(namespace) || !exists(symbol, envir=namespace, inherits=FALSE)) inspection_error("object_not_found")
    if (bindingIsActive(symbol, namespace)) inspection_error("active_binding_unsupported")
    value <- get(symbol, envir=namespace, inherits=FALSE)
    object <- inspection_object(value, request)
    axes <- list()
    readers <- list()
    warnings <- new.env(parent=emptyenv())
    warnings$items <- character()
    for (index in seq_along(c("row", "column"))) {
      axis <- c("row", "column")[[index]]
      spec <- request[[paste0(axis, "Identity")]]
      keys <- if (spec$kind == "index") "index" else inspection_keys(spec$names)
      entry <- list(axis=axis, role=object$roles[[index]], count=as.integer(object$shape[[index]]), keys=as.list(keys), idsFile=NULL, reason="")
      identity <- tryCatch(inspection_axis(output_directory, axis, object$metadata[[index]], spec), error=function(error) error)
      if (inherits(identity, "error")) {
        reason <- conditionMessage(identity)
        allowed <- c("invalid_identity", "identity_missing", "identity_limit", "identity_columns_missing_or_ambiguous")
        if (!reason %in% allowed) stop(identity)
        entry$reason <- reason
      } else {
        entry$idsFile <- paste0(axis, ".ids")
        readers[[axis]] <- identity
      }
      axes[[index]] <- entry
    }
    table <- NULL
    if (length(readers) == 2L) {
      axis <- request$tableAxis
      index <- match(axis, c("row", "column"))
      table <- inspection_table(output_directory, axis, object$metadata[[index]], readers[[axis]], warnings)
    }
    result <- list(schemaVersion=1L, status="ok", type=object$type, shape=as.list(as.integer(object$shape)), axes=axes, matrix=object$matrix, table=table)
    if (length(warnings$items)) result$warnings <- as.list(warnings$items)
    result
  }, error=function(error) {
    allowed <- c("invalid_request", "invalid_symbol", "unsupported_adapter", "unsupported_type", "invalid_dimensions", "object_not_found", "active_binding_unsupported", "table_limit", "publish_failed")
    reason <- conditionMessage(error)
    if (!reason %in% allowed) reason <- "inspection_failed"
    list(schemaVersion=1L, status="error", reason=reason)
  })
  inspection_atomic_json(file.path(output_directory, "inspection.json"), result)
  result
}

write_selection <- function(keys, rows, output_directory) {
  keys <- identity_keys(keys)
  if (!is.list(rows) || length(rows) < 1L) inspection_error("empty_selection")
  if (length(rows) > 1000000L) inspection_error("identity_limit")
  seen <- new.env(hash=TRUE, parent=emptyenv())
  row_at <- function(index) {
    row <- rows[[index]]
    encoded <- identity_tuple(row)
    if (exists(encoded, envir=seen, inherits=FALSE)) inspection_error("duplicate_identity")
    assign(encoded, TRUE, envir=seen)
    row
  }
  name <- paste0("selection-", inspection_token())
  path <- file.path(output_directory, paste0(name, ".ids"))
  temporary <- tempfile(pattern="selection-", tmpdir=output_directory)
  complete <- FALSE
  on.exit({ unlink(temporary); if (!complete) unlink(path) }, add=TRUE)
  connection <- file(temporary, "wb")
  count <- tryCatch(identity_write_iter(connection, keys, length(rows), row_at), finally=close(connection))
  if (!file.rename(temporary, path)) inspection_error("publish_failed")
  descriptor <- list(schemaVersion=1L, file=basename(path), keys=as.list(keys), count=count)
  inspection_atomic_json(file.path(output_directory, paste0(name, ".json")), descriptor)
  complete <- TRUE
  descriptor
}

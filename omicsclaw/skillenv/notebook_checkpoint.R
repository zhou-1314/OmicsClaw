# Fixed object serialization. The owner supplies every symbol, path and limit.
checkpoint_plain <- function(value, depth=0L, dispersion=FALSE) {
  if (depth > 48L) stop("unsupported_serializer", call.=FALSE)
  type <- typeof(value)
  if (type %in% c("environment", "externalptr", "weakref")) stop("unsupported_serializer", call.=FALSE)
  if (type %in% c("closure", "builtin", "special")) {
    if (!dispersion) stop("unsupported_serializer", call.=FALSE)
    return(invisible(NULL))
  }
  if (inherits(value, "formula")) {
    # Formula environments are references, not additional saved namespaces.
    if (!identical(environment(value), globalenv()) && !identical(environment(value), baseenv()))
      stop("unsupported_formula_environment", call.=FALSE)
    return(invisible(NULL))
  }
  if (isS4(value)) {
    package <- attr(class(value), "package")
    if (length(package) != 1L || !package %in% c("DESeq2", "SummarizedExperiment", "S4Vectors", "IRanges", "GenomicRanges", "GenomeInfoDb"))
      stop("unsupported_serializer", call.=FALSE)
    for (name in methods::slotNames(value))
      checkpoint_plain(methods::slot(value,name), depth+1L,
        identical(class(value)[[1]],"DESeqDataSet") && name == "dispersionFunction")
  } else {
    if (is.list(value) || is.pairlist(value)) for (item in value) checkpoint_plain(item,depth+1L)
    for (item in attributes(value)) checkpoint_plain(item,depth+1L)
  }
  invisible(NULL)
}

checkpoint_reference <- function(value) {
  if (typeof(value) %in% c("externalptr", "weakref")) stop("unsupported_serializer",call.=FALSE)
  if (is.environment(value)) {
    names <- ls(value, all.names=TRUE)
    if (length(names) > 4096L || any(vapply(names, function(name) bindingIsActive(name,value), logical(1))))
      stop("unsupported_active_environment",call.=FALSE)
  }
  NULL
}

checkpoint_kind <- function(value) {
  kind <- class(value)
  if (identical(kind,"data.frame")) return("R.data.frame")
  if (identical(kind,c("matrix","array"))) return("R.matrix")
  if (length(kind)==1L && kind %in% c("DESeqDataSet","SummarizedExperiment","RangedSummarizedExperiment") &&
      requireNamespace("SummarizedExperiment",quietly=TRUE) && methods::is(value,"SummarizedExperiment"))
    return(paste0("R.",kind))
  stop("unsupported_serializer",call.=FALSE)
}

checkpoint_save <- function(namespace, request, directory, python, writer) {
  if (!is.list(request$objects) || length(request$objects)<1L || length(request$objects)>32L ||
      length(request$maxFileBytes)!=1L || length(request$maxBytes)!=1L ||
      !is.finite(request$maxFileBytes) || !is.finite(request$maxBytes) ||
      request$maxFileBytes<1 || request$maxBytes<1) stop("invalid_checkpoint_request",call.=FALSE)
  objects <- list(); symbols <- character(); keys <- character()
  for (entry in request$objects) {
    key <- entry$logicalKey; symbol <- entry$symbol
    if (!is.character(key) || length(key)!=1L || !grepl("^[A-Za-z0-9_-]{1,64}$",key) ||
        !is.character(symbol) || length(symbol)!=1L ||
        !grepl("^(?:[A-Za-z]|\\.(?![0-9]))[A-Za-z0-9._]*$",symbol,perl=TRUE) ||
        key %in% keys || symbol %in% symbols || !identical(entry$adapter,"r_rds") ||
        !exists(symbol,namespace,inherits=FALSE) || bindingIsActive(symbol,namespace))
      stop("invalid_checkpoint_binding",call.=FALSE)
    value <- get(symbol,namespace,inherits=FALSE)
    kind <- checkpoint_kind(value)
    checkpoint_plain(value)
    objects[[length(objects)+1L]] <- list(entry=entry,value=value,kind=kind)
    keys <- c(keys,key); symbols <- c(symbols,symbol)
  }
  total <- 0; result <- list()
  for (object in objects) {
    path <- file.path(directory,paste0(object$entry$logicalKey,".rds"))
    limit <- min(request$maxFileBytes,request$maxBytes-total)
    if (limit<1 || file.exists(path)) stop("checkpoint_byte_limit",call.=FALSE)
    command <- paste(shQuote(python),"-I",shQuote(writer),format(limit,scientific=FALSE,trim=TRUE),shQuote(path))
    connection <- pipe(command,"wb")
    tryCatch(saveRDS(object$value,connection,compress=FALSE,version=3,refhook=checkpoint_reference),
             finally=suppressWarnings(close(connection)))
    if (!file.exists(path)) stop("checkpoint_write_failed",call.=FALSE)
    bytes <- file.info(path)$size
    if (!is.finite(bytes) || bytes>limit) stop("checkpoint_byte_limit",call.=FALSE)
    total <- total+bytes
    result[[length(result)+1L]] <- c(object$entry,list(type=object$kind,format="rds3",file=basename(path),
      bytes=bytes,shape=as.list(dim(object$value)),serializerVersion=1L))
  }
  raw <- charToRaw(jsonlite::toJSON(list(schemaVersion=1L,objects=result,totalBytes=total),
    auto_unbox=TRUE,null="null",digits=NA))
  if (length(raw)>32768L) stop("checkpoint_manifest_limit",call.=FALSE)
  temporary <- file.path(directory,"saved.partial")
  con <- file(temporary,"wb");tryCatch(writeBin(raw,con),finally=close(con))
  if (!file.rename(temporary,file.path(directory,"saved.json"))) stop("checkpoint_publish_failed",call.=FALSE)
  invisible(NULL)
}

checkpoint_verify <- function(path, expected_shape) {
  value <- readRDS(path)
  checkpoint_kind(value);checkpoint_plain(value)
  if (!identical(as.integer(dim(value)),as.integer(expected_shape))) stop("checkpoint_shape_mismatch",call.=FALSE)
  value
}

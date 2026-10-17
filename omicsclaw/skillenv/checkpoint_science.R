# Private fixed operations; no discovery or mutation of user namespaces.
checkpoint_runtime_evidence <- function(directory) {
  packages <- list()
  for(name in c("DESeq2","SummarizedExperiment","S4Vectors","IRanges","GenomicRanges","GenomeInfoDb","BiocGenerics","Biobase","Matrix","jsonlite")) {
    packages[name] <- list(if(name %in% loadedNamespaces()) list(
      version=as.character(getNamespaceVersion(name)),path=getNamespaceInfo(asNamespace(name),"path")) else NULL)
  }
  evidence <- list(schemaVersion=1L,language="r",runtimeVersion=R.version.string,
    executable=file.path(R.home("bin"),"R"),prefix=R.home(),libraryPaths=as.list(.libPaths()),packages=packages)
  raw <- charToRaw(jsonlite::toJSON(evidence,auto_unbox=TRUE,null="null",digits=NA))
  if(length(raw)>32768L) stop("checkpoint_manifest_limit",call.=FALSE)
  temporary <- file.path(directory,"runtime.partial")
  con <- file(temporary,"wb");tryCatch(writeBin(raw,con),finally=close(con))
  if(!file.rename(temporary,file.path(directory,"runtime.json"))) stop("checkpoint_publish_failed",call.=FALSE)
  invisible(NULL)
}

checkpoint_verify_group <- function(files,directory,root) {
  serializer <- new.env(parent=baseenv())
  sys.source(file.path(root,"notebook_checkpoint.R"),envir=serializer)
  for (item in files) {
    path <- file.path(directory,item$file)
    check <- function() {
      if (!identical(unname(tools::md5sum(path)),item$md5) || file.info(path)$size!=item$bytes)
        stop("checkpoint_file_changed",call.=FALSE)
    }
    check();value <- serializer$checkpoint_verify(path,unlist(item$shape));rm(value);check()
  }
  invisible(NULL)
}

checkpoint_inspect_group <- function(namespace, requests, directory, maximum, root) {
  inspector <- new.env(parent=baseenv())
  sys.source(file.path(root,"notebook_identity.R"),envir=inspector)
  sys.source(file.path(root,"notebook_inspect.R"),envir=inspector)
  remaining <- maximum
  inspector$writeBin <- function(object, con, ...) {
    if (!is.raw(object) || length(object)>remaining) stop("checkpoint_byte_limit",call.=FALSE)
    remaining <<- remaining-length(object)
    base::writeBin(object,con,...)
  }
  for (item in requests) {
    destination <- file.path(directory,item$logicalKey)
    if (!dir.create(destination,mode="0700")) stop("checkpoint_directory_failed",call.=FALSE)
    inspector$inspect_object(namespace,item$request,destination)
  }
  invisible(NULL)
}

checkpoint_stage_writer <- function(directory) {
  force(directory)
  function(stage,metadata=NULL) {
    if (!is.character(stage) || length(stage)!=1L || !stage %in% c("qc","normalization","integration","clustering","annotation","differential_expression"))
      stop("invalid_checkpoint_stage",call.=FALSE)
    validate <- function(value,depth=0L) {
      if (depth>8L || is.object(value) || is.environment(value)) stop("invalid_checkpoint_stage_metadata",call.=FALSE)
      if (is.null(value)) return(invisible(NULL))
      if (is.list(value) && length(value)<=100L) {
        for (item in value) validate(item,depth+1L)
      } else if (!(typeof(value) %in% c("logical","integer","double","character")) || length(value)>100L || anyNA(value) || (is.numeric(value) && any(!is.finite(value))))
        stop("invalid_checkpoint_stage_metadata",call.=FALSE)
    }
    validate(metadata)
    raw <- charToRaw(jsonlite::toJSON(list(schemaVersion=1L,stage=stage,ordinal=1L,metadata=metadata),auto_unbox=TRUE,null="null",digits=NA))
    if(length(raw)>4096L) stop("checkpoint_stage_metadata_limit",call.=FALSE)
    path <- file.path(directory,"stage.json")
    if(file.exists(path)) {
      file.create(file.path(directory,"duplicate"));stop("one_stage_per_execution",call.=FALSE)
    }
    temporary <- file.path(directory,"stage.partial")
    con <- file(temporary,"wb");tryCatch(writeBin(raw,con),finally=close(con))
    if(!file.rename(temporary,path)) stop("checkpoint_stage_write_failed",call.=FALSE)
    invisible(NULL)
  }
}

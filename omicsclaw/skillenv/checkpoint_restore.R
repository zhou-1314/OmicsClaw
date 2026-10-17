# Private whole-group readers. The owner fixes all paths and bindings.
restore_prepare <- function(namespace, request, directory, root) {
  fixed <- new.env(parent=baseenv())
  sys.source(file.path(root,"checkpoint_science.R"),envir=fixed)
  sys.source(file.path(root,"notebook_checkpoint.R"),envir=fixed)
  source <- request$runtimeEvidence
  for(name in names(source$packages)) if(!is.null(source$packages[[name]])) loadNamespace(name)
  fixed$checkpoint_runtime_evidence(directory)
  version <- function(x) regmatches(x,regexpr("[0-9]+[.][0-9]+",x))
  if(!identical(version(source$runtimeVersion),version(R.version.string))) stop("restore_runtime_incompatible",call.=FALSE)
  for(name in names(source$packages)) {
    evidence <- source$packages[[name]]
    if(!is.null(evidence) && (is.null(evidence$version) || !identical(as.character(getNamespaceVersion(name)),evidence$version)))
      stop("restore_dependency_incompatible",call.=FALSE)
  }
  if(exists(request$token,namespace,inherits=FALSE)) stop("restore_target_consumed",call.=FALSE)
  values <- new.env(parent=emptyenv())
  for(item in request$files) {
    symbol <- item$symbol
    if(exists(symbol,namespace,inherits=FALSE) || exists(symbol,values,inherits=FALSE)) stop("restore_symbol_conflict",call.=FALSE)
    check <- function() {
      if(file.info(item$path)$size!=item$bytes || !identical(unname(tools::md5sum(item$path)),item$md5))
        stop("restore_source_changed",call.=FALSE)
    }
    check();value <- fixed$checkpoint_verify(item$path,unlist(item$shape));check()
    assign(symbol,value,values)
  }
  assign(request$token,values,namespace)
  fixed$checkpoint_inspect_group(values,request$inspection,directory,request$maxStagingBytes,root)
  invisible(NULL)
}

restore_commit <- function(namespace,token,directory,root) {
  if(!exists(token,namespace,inherits=FALSE) || bindingIsActive(token,namespace)) stop("restore_target_changed",call.=FALSE)
  values <- get(token,namespace,inherits=FALSE)
  if(!is.environment(values)) stop("restore_target_changed",call.=FALSE)
  symbols <- ls(values,all.names=TRUE)
  if(!length(symbols) || any(vapply(symbols,exists,logical(1),envir=namespace,inherits=FALSE))) stop("restore_target_changed",call.=FALSE)
  for(symbol in symbols) assign(symbol,get(symbol,values,inherits=FALSE),namespace)
  rm(list=token,envir=namespace)
  path <- file.path(directory,"committed.json")
  raw <- charToRaw(jsonlite::toJSON(list(token=token),auto_unbox=TRUE))
  con <- file(paste0(path,".partial"),"wb");tryCatch(writeBin(raw,con),finally=close(con))
  if(!file.rename(paste0(path,".partial"),path)) stop("restore_commit_record_failed",call.=FALSE)
  invisible(NULL)
}

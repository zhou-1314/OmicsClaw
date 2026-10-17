# Canonical identity bytes; the owner computes digests from these exact files.
identity_text <- function(value, maximum=512L) {
  if (!is.character(value) || length(value) != 1L || is.na(value)) stop("invalid_identity")
  value <- enc2utf8(value)
  if (is.na(iconv(value, from="UTF-8", to="UTF-8", sub=NA_character_))) stop("invalid_identity")
  points <- utf8ToInt(value)
  if (any(points == 0L) || any(points >= 55296L & points <= 57343L)) stop("invalid_identity")
  if (length(charToRaw(value)) > maximum) stop("identity_limit")
  value
}

identity_keys <- function(values) {
  if (!is.character(values) || length(values) < 1L || length(values) > 4L) stop("invalid_identity")
  values <- vapply(values, identity_text, character(1), maximum=128L, USE.NAMES=FALSE)
  if (anyDuplicated(values)) stop("invalid_identity")
  values
}

identity_tuple <- function(values) {
  if (!is.character(values) || length(values) < 1L || length(values) > 4L) stop("invalid_identity")
  quote <- function(value) {
    points <- utf8ToInt(identity_text(value))
    pieces <- vapply(points, function(point) {
      if (point == 34L) return('\\"')
      if (point == 92L) return('\\\\')
      if (point < 32L) return(sprintf("\\u%04x", point))
      intToUtf8(point)
    }, character(1), USE.NAMES=FALSE)
    paste0('"', paste0(pieces, collapse=""), '"')
  }
  paste0("[", paste0(vapply(values, quote, character(1), USE.NAMES=FALSE), collapse=","), "]")
}

identity_header <- function(keys) {
  paste0('{"schemaVersion":1,"keys":', identity_tuple(identity_keys(keys)), '}\n')
}

identity_write_iter <- function(connection, keys, count, row_at) {
  keys <- identity_keys(keys)
  if (length(count) != 1L || is.na(count) || count < 0L || count > 1000000L) stop("identity_limit")
  first <- charToRaw(identity_header(keys))
  writeBin(first, connection)
  bytes <- length(first)
  for (index in seq_len(count)) {
    row <- row_at(index)
    if (!is.character(row) || length(row) != length(keys)) stop("invalid_identity")
    line <- charToRaw(paste0(identity_tuple(row), "\n"))
    bytes <- bytes + length(line)
    if (bytes > 33554432L) stop("identity_limit")
    writeBin(line, connection)
  }
  as.integer(count)
}

identity_write <- function(connection, keys, rows) {
  if (!is.list(rows)) stop("invalid_identity")
  identity_write_iter(connection, keys, length(rows), function(index) rows[[index]])
}

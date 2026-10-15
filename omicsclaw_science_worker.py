"""Scientific process bootstrap binding the original SDK at the payload boundary."""

from omicsclaw.entry.ssh.science_worker import main


def write_result(value, path, suffix):
    from skills._sdk.notebook._io import _default_writer

    _default_writer(value, path, suffix)


if __name__ == "__main__":
    main(result_writer=write_result)

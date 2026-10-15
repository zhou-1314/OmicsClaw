"""Launch a verified scientific payload in its selected worker process."""

from omicsclaw.entry.ssh.science_worker import main, write_result

if __name__ == "__main__":
    main(result_writer=write_result)

"""Local CPU receipt-owner lock; never removes a potentially locked inode.

This excludes other owners of the canonical log path, not Router dispatchers
or remote hosts. Supported only for the isolated local-filesystem experiment.
"""

import os


class LedgerOwnership:
    def __init__(self, database):
        self.stream = database.with_name(database.name + ".owner").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                if self.stream.seek(0, os.SEEK_END) == 0:
                    self.stream.write(b"\0")
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.stream.close()
            raise

    def close(self):
        # Closing the descriptor releases its lock, including on process death.
        self.stream.close()

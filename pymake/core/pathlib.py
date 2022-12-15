import pathlib

class Path(type(pathlib.Path())):

    @property
    def modification_time(self):
        return self.stat().st_mtime

